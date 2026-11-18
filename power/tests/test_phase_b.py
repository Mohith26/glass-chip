"""Tests for the pieces added for the real-core studies (s06-s08)."""
import pytest

from power.cosim_hooks import ObservedChip, make_event_hook
from power.manager import PowerManager
from power.params import load_params, load_policy, load_soc
from power.predictor import ExecTimePredictor
from power.tests.fakesim import MS, make_fakesim, run_closed_loop, synthetic_tasks, task_info
from power.thermal_analysis import open_loop_onset, steady_state_with_leakage

SOC = load_soc()


def _pm(policy="P0", **kw):
    return PowerManager(load_policy(policy), SOC, 25.0, **kw)


def _nonleak(pm, w=0.3):
    return {d: w for d in pm.domains}, {d: pm.vf[d].nominal.volts for d in pm.domains}


def test_steady_state_with_leakage_hotter_than_without_and_detects_runaway():
    pm = _pm()
    pnl, volts = _nonleak(pm)
    ss = steady_state_with_leakage(pm, pnl, volts, 25.0)
    assert not ss["runaway"]
    no_leak = pm.thermal.steady_state(pnl)
    assert ss["t_max_c"] > max(no_leak[pm.thermal.idx[d]] for d in pm.domains)
    assert ss["leakage_w"] > 0
    hot = steady_state_with_leakage(pm, {d: 3.0 for d in pm.domains}, volts, 60.0)
    assert hot["runaway"]


def test_open_loop_onset():
    pm = _pm()
    pnl, volts = _nonleak(pm, 0.05)
    assert open_loop_onset(pm, pnl, volts, 25.0, 100.0, t_max_s=600)["onset_s"] is None
    pnl_hot, _ = _nonleak(pm, 1.5)
    o = open_loop_onset(pm, pnl_hot, volts, 45.0, 100.0, t_max_s=4 * 3600)
    assert o["onset_s"] is not None and o["onset_s"] > 10


def test_slow_node_time_scale_divides_slow_capacitances():
    from power.thermal import build_soc_thermal
    p = load_params()
    a = build_soc_thermal(SOC, p, ("npu",), 25.0)
    p2 = load_params(overrides={"thermal": {"slow_node_time_scale": 100.0}})
    b = build_soc_thermal(SOC, p2, ("npu",), 25.0)
    assert b.C[b.idx["enclosure"]] == pytest.approx(a.C[a.idx["enclosure"]] / 100)
    assert b.C[b.idx["npu"]] == pytest.approx(a.C[a.idx["npu"]])


def test_settle_resets_accounting_and_jumps_temperature():
    tasks = synthetic_tasks(0.8)
    pm = _pm("P0", task_info=task_info(tasks), thermal_init="settle")
    s = run_closed_loop(make_fakesim(pm, tasks, seed=0), pm, 1500 * MS)
    assert s["settle"] is not None and s["settle"]["t_s"] == pytest.approx(1.0)
    assert s["duration_s"] == pytest.approx(0.5, abs=2e-3)
    assert s["accounted_from_s"] == pytest.approx(1.0)
    assert s["peak_temp_c"] > 30.0


def test_predictor_critical_path_model():
    pr = ExecTimePredictor(min_samples=2, quantile=0.5)
    freqs = {"npu": 1.0e9, "cpu_a": 2.0e9}
    for k in range(3):
        pr.observe_pending([{"job_id": k, "task_id": "T", "release_ps": k * 10**10, "deadline_ps": k * 10**10 + 10**11,
                             "started": False, "frac_done": 0.0,
                             "est_remaining_cycles": {"npu": 1e6, "cpu_a": 2e6}}])
        pr.observe_completion({"job_id": k, "task_id": "T", "release_ps": k * 10**10, "deadline_ps": k * 10**10 + 10**11,
                               "response_ps": 5 * 10**9,
                               "attribution": {"compute": 2 * 10**9, "runqueue": 10**9, "dram": 2 * 10**9},
                               "cycles_by_domain": {"npu": 1e6, "cpu_a": 2e6}}, freqs)
    assert pr.has_time_model("T")
    # compute 2 ms split by cycle-time share (1 ms on each domain), converted to cycles
    assert pr.cpc_cycles("T", "npu") == pytest.approx(1e-3 * 1e9)
    assert pr.cpc_cycles("T", "cpu_a") == pytest.approx(1e-3 * 2e9)
    # M = response - compute - queueing = 5 - 2 - 1 ms
    assert pr.fixed_time_ps("T") == pytest.approx(2 * 10**9)
    job = {"job_id": 99, "task_id": "T", "est_remaining_cycles": {"npu": 1e6}, "frac_done": 0.0, "started": False,
           "release_ps": 0, "deadline_ps": 10**11}
    pr.observe_pending([job])
    assert pr.remaining_fraction(dict(job, est_remaining_cycles={"npu": 2.5e5}), "npu") == pytest.approx(0.25)


def test_p4_keeps_infrastructure_domain_at_nominal():
    tasks = synthetic_tasks(0.8)
    pm = _pm("P4", task_info=task_info(tasks))
    run_closed_loop(make_fakesim(pm, tasks, seed=0), pm, 200 * MS)
    assert pm.opp["noc_cache"] == pm.vf["noc_cache"].nominal_index
    cfg = load_policy("P4")
    cfg["params"] = dict(cfg["params"], govern_infrastructure=True)
    pm2 = PowerManager(cfg, SOC, 25.0, task_info=task_info(tasks))
    run_closed_loop(make_fakesim(pm2, tasks, seed=0), pm2, 200 * MS)
    assert pm2.opp["noc_cache"] < pm2.vf["noc_cache"].nominal_index    # FakeSim has no NoC traffic


def test_gating_hysteresis_passed_when_supported():
    calls = {}

    class S:
        def set_power_gating(self, d, en, wake, idle_before_gate_ps=0):
            calls[d] = (en, wake, idle_before_gate_ps)

        def counters(self, reset=True):
            return {}

        def pending_jobs(self):
            return []

        def set_domain_freq(self, d, f):
            pass

    pm = _pm("P3")
    pm.step(S(), 0)
    assert calls["npu"] == (True, pm.gating.wake_latency_ps, pm.gating.idle_before_gate_ps)
    assert pm.gating.idle_before_gate_ps == int(load_params()["gating"]["idle_before_gate_us"] * 1e6)


def test_cosim_hooks():
    seen = []

    class PM:
        amb = None
        vmin = None

        def observe_completions(self, c):
            seen.extend(c)

        def set_ambient(self, a):
            PM.amb = a

        def set_vmin(self, d, v):
            PM.vmin = (d, v)

    class Chip:
        x = 5

        def run_until(self, t):
            return [{"job_id": 1}]

    pm = PM()
    ch = ObservedChip(Chip(), pm)
    assert ch.run_until(10) == [{"job_id": 1}] and seen == [{"job_id": 1}] and ch.x == 5
    chained = []
    hook = make_event_hook(pm, chained=lambda e, c: chained.append(e["action"]))
    hook({"action": "set_ambient", "args": {"ambient_c": 41}}, None)
    hook({"action": "set_vmin", "args": {"domain": "npu", "v_min": 0.6}}, None)
    assert PM.amb == 41 and PM.vmin == ("npu", 0.6) and chained == ["set_ambient", "set_vmin"]


def test_avg_freq_integrates_opp_history():
    class S:
        def counters(self, reset=True):
            return {}

        def pending_jobs(self):
            return []

        def set_domain_freq(self, d, f):
            pass

    pm = _pm("P0")
    sim = S()
    pm.step(sim, 0)
    f_nom = pm.vf["npu"].nominal.freq_hz
    pm.step(sim, MS)
    pm.set_vmin("npu", 0.5)           # P0 pin -> next step runs npu at 0.5 V
    pm.step(sim, 2 * MS)
    pm.step(sim, 3 * MS)
    f_low = pm.vf["npu"].opps[pm.opp["npu"]].freq_hz
    assert pm.avg_freq("npu", 0, 2 * MS) == pytest.approx(f_nom)
    assert pm.avg_freq("npu", 2 * MS, 3 * MS) == pytest.approx(f_low)
    assert pm.avg_freq("npu", MS, 3 * MS) == pytest.approx((f_nom + f_low) / 2)


def test_cpc_anchor_keeps_nominal_samples():
    pr = ExecTimePredictor(min_samples=1, quantile=0.5, window=3, nominal_freqs={"npu": 1e9})
    def comp(k, f):
        pr.observe_completion({"job_id": k, "task_id": "T", "release_ps": 0, "deadline_ps": 10**12,
                               "response_ps": 10**9, "attribution": {"compute": 10**9},
                               "cycles_by_domain": {"npu": 1e6}}, {"npu": f})
    comp(0, 1e9)                       # anchor sample: 1 ms x 1 GHz = 1e6 cycles
    for k in range(1, 6):
        comp(k, 0.5e9)                 # low-clock samples: 0.5e6 cycles each, evict the window
    assert pr.cpc_cycles("T", "npu") == pytest.approx(1e6)
