"""Policy tests on the SYNTHETIC FakeSim job set (not captured workloads)."""

import pytest

from power.manager import PowerManager
from power.offline_opt import solve_trace
from power.params import load_policy, load_soc
from power.tests.fakesim import MS, make_fakesim, run_closed_loop, synthetic_tasks, task_info

SOC = load_soc()
DUR = 600 * MS


def run(policy, scale=0.8, seed=0, duration=DUR, **kw):
    tasks = synthetic_tasks(scale)
    pm = PowerManager(load_policy(policy), SOC, 25.0, task_info=task_info(tasks), **kw)
    sim = make_fakesim(pm, tasks, seed=seed)
    summary = run_closed_loop(sim, pm, duration)
    return pm, sim, summary


@pytest.fixture(scope="module")
def runs():
    return {p: run(p) for p in ("P0", "P3", "P4")}


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_p4_never_misses_where_nominal_does_not(seed):
    _, _, s0 = run("P0", seed=seed)
    assert s0["misses_total"] == 0, "precondition: nominal meets every deadline on this synthetic set"
    _, _, s4 = run("P4", seed=seed)
    assert s4["misses_total"] == 0
    assert s4["safety_misses"] == 0
    assert s4["energy_j"] < s0["energy_j"]


def test_energy_ordering_p5_le_p4_le_p0(runs):
    pm0, _, s0 = runs["P0"]
    pm4, _, s4 = runs["P4"]
    bound = solve_trace(pm0.job_trace, pm0, time_limit_s=20)
    assert all(v["unmet_cycles"] < 1e-6 for v in bound["per_domain"].values())
    assert bound["energy_j"] <= s4["energy_j"] <= s0["energy_j"]
    # the bound is also below race to idle
    assert bound["energy_j"] <= runs["P3"][2]["energy_j"]


def test_p5_bound_independent_of_recording_policy(runs):
    pm0, pm4 = runs["P0"][0], runs["P4"][0]
    b0 = solve_trace(pm0.job_trace, pm0, time_limit_s=20)
    b4 = solve_trace(pm4.job_trace, pm4, time_limit_s=20)
    # same jobs (same seed), same model: bounds agree closely (jobs finishing after horizon differ slightly)
    assert b4["energy_j"] == pytest.approx(b0["energy_j"], rel=0.05)


def test_p3_race_to_idle_closed_loop(runs):
    pm3, sim3, s3 = runs["P3"]
    s0 = runs["P0"][2]
    assert s3["misses_total"] == 0
    assert s3["wakeups"] > 0
    for d, dvf in pm3.vf.items():
        if not dvf.fixed:
            assert pm3.opp[d] == dvf.max_index
    gated = sum(sum(v) for d, v in pm3.series["gated_frac"].items())
    assert gated > 0
    assert s3["energy_j"] < s0["energy_j"]
    assert s3["energy_j_by_part"]["wake"] > 0


class StubSim:
    """Returns hand-written counters so the accounting can be checked analytically."""

    def __init__(self, counters):
        self._c = counters
        self.freqs = {}
        self.gating = {}

    def counters(self, reset=True):
        return self._c

    def pending_jobs(self):
        return []

    def set_domain_freq(self, d, f):
        self.freqs[d] = f

    def domain_freq(self, d):
        return self.freqs.get(d)

    def set_power_gating(self, d, enabled, wake_ps):
        self.gating[d] = (enabled, wake_ps)


def test_p3_accounting_gated_idle_and_wake():
    pm = PowerManager(load_policy("P3"), SOC, 25.0)
    dt = MS
    busy = 0.4 * dt
    counters = {"npu": {"busy_ps": busy, "idle_ps": dt - busy, "gated_ps": dt - busy - 10_000_000, "wakeups": 1,
                        "int8_macs": 1e9}}
    sim = StubSim(counters)
    pm.step(sim, 0)                      # first call: enables gating, picks max OPP
    assert sim.gating["npu"] == (True, pm.gating.wake_latency_ps)
    assert sim.gating.get("rt") is None  # safety island is not gateable
    pm.energy_by_domain_j.clear()
    pm._pending_transition.clear()
    pm.step(sim, dt)
    o = pm.vf["npu"].opps[pm.vf["npu"].max_index]
    v, f = o.volts, o.freq_hz
    gated = dt - busy - 10_000_000
    on = dt - gated
    leak = pm.leakage.power_w("npu", v, 25.0)
    e = (pm.energy.dynamic_energy_j("npu", counters["npu"], v)[0]
         + pm.energy.clock_power_w("npu", v, f) * on / 1e12
         + leak * on / 1e12 + leak * pm.gating.retention_leak_fraction * gated / 1e12
         + pm.gating.wake_energy_j(v))
    p_out = e / (dt / 1e12)
    expected = pm.regulators["npu"].input_w(p_out) * dt / 1e12
    assert pm.energy_by_domain_j["npu"] == pytest.approx(expected, rel=1e-12)


def test_p3_fully_gated_domain_draws_only_retention():
    pm = PowerManager(load_policy("P3"), SOC, 25.0)
    sim = StubSim({"simt": {"busy_ps": 0, "idle_ps": MS, "gated_ps": MS, "wakeups": 0}})
    pm.step(sim, 0)
    pm.energy_by_domain_j.clear()
    pm._pending_transition.clear()
    pm.step(sim, MS)
    v = pm.vf["simt"].opps[-1].volts
    p_ret = pm.leakage.power_w("simt", v, 25.0) * pm.gating.retention_leak_fraction
    assert pm.energy_by_domain_j["simt"] == pytest.approx(pm.regulators["simt"].input_w(p_ret) * 1e-3, rel=1e-12)


def test_p6_without_surrogate_equals_p4():
    _, _, s4 = run("P4", duration=300 * MS)
    _, _, s6 = run("P6", duration=300 * MS)
    assert s6["energy_j"] == pytest.approx(s4["energy_j"], rel=1e-12)
    assert s6["misses_total"] == s4["misses_total"] == 0


def test_p6_with_oracle_surrogate_meets_deadlines():
    def surrogate(job, domain, f_hz, now_ps):
        rem = sum(job["est_remaining_cycles"].values()) * 1.3
        return now_ps + rem / f_hz * 1e12 * 2.0      # loose upper bound

    tasks = synthetic_tasks(0.8)
    pm = PowerManager(load_policy("P6"), SOC, 25.0, task_info=task_info(tasks), surrogate=surrogate)
    s = run_closed_loop(make_fakesim(pm, tasks, seed=0), pm, 300 * MS)
    assert s["misses_total"] == 0


def test_scheduler_exposed_and_applied():
    for p, sched in (("P0", "fifo"), ("P1", "fixed_priority"), ("P2", "edf"), ("P4", "edf")):
        pm = PowerManager(load_policy(p), SOC, 25.0)
        assert pm.scheduler == sched
    calls = []

    class S:
        def set_scheduler(self, e, pol):
            calls.append((e, pol))

    PowerManager(load_policy("P1"), SOC, 25.0).apply_scheduler(S())
    assert ("rt", "fixed_priority") in calls and len(calls) == 6


def test_nominal_policies_run_at_nominal(runs):
    pm0 = runs["P0"][0]
    for d, dvf in pm0.vf.items():
        assert pm0.opp[d] == dvf.nominal_index


def test_set_vmin_pins_static_and_floors_dynamic():
    pm0 = PowerManager(load_policy("P0"), SOC, 25.0)
    pm0.set_vmin("cpu_a", 0.6)
    sim = StubSim({})
    pm0.step(sim, 0)
    assert pm0.vf["cpu_a"].opps[pm0.opp["cpu_a"]].volts == pytest.approx(0.6)
    assert sim.freqs["cpu_a"] == pytest.approx(pm0.vf["cpu_a"].curve.freq(0.6))
    pm4 = PowerManager(load_policy("P4"), SOC, 25.0)
    pm4.set_vmin("cpu_a", 0.65)
    pm4.step(StubSim({}), 0)
    assert pm4.vf["cpu_a"].opps[pm4.opp["cpu_a"]].volts >= 0.65 - 1e-9


def test_set_vmin_below_table_is_flagged():
    pm = PowerManager(load_policy("P0"), SOC, 25.0)
    pm.set_vmin("npu", 0.40)
    pm.step(StubSim({}), 0)
    assert pm.vf["npu"].opps[pm.opp["npu"]].extrapolated
    assert any("extrapolated" in n for n in pm.summary()["labels"])


def test_low_voltage_toggle_labels_hypothetical():
    pm = PowerManager(load_policy("P4"), SOC, 25.0, low_voltage_arith={"enabled": True, "reduction_x": 2.0})
    assert pm.summary()["hypothetical"] is True
    assert pm.vf["npu"].opps[0].volts == pytest.approx(0.40)
    pm1 = PowerManager(load_policy("P4"), SOC, 25.0)
    assert pm1.summary()["hypothetical"] is False


def test_hot_ambient_throttles_and_records_series():
    tasks = synthetic_tasks(0.8)
    pm = PowerManager(load_policy("P3"), SOC, 60.0, task_info=task_info(tasks), thermal_init="steady",
                      warm_start_w=9.0)
    s = run_closed_loop(make_fakesim(pm, tasks, seed=0), pm, 200 * MS)
    assert s["time_throttled_s"] > 0
    assert s["peak_temp_c"] > 100
    arr = pm.series_arrays()
    assert len(arr["t_s"]) == len(arr["power_in_w"]) == len(arr["temp_c"]["npu"])
    assert arr["energy_j"][-1] == pytest.approx(s["energy_j"])
