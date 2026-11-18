import math

import pytest

from power.dvfs import RegulatorModel, transition_energy_j
from power.params import load_params, load_soc
from power.thermal import ThermalRC, ThrottleController, build_soc_thermal

MS = 1_000_000_000


def test_single_node_steady_state_matches_r_times_p():
    r, c, amb, p = 2.5, 0.4, 30.0, 7.0
    net = ThermalRC(["die"], [c], [("die", "ambient", r)], amb)
    assert net.steady_state({"die": p})[0] == pytest.approx(amb + r * p)
    for _ in range(int(20 * r * c / 1e-3)):        # 20 time constants of 1 ms implicit steps
        net.step({"die": p}, 1e-3)
    assert net.temp("die") == pytest.approx(amb + r * p, rel=1e-6)


def test_implicit_step_is_stable_for_large_dt():
    net = ThermalRC(["die"], [0.001], [("die", "ambient", 1.0)], 25.0)   # tau = 1 ms
    temps = [net.step({"die": 10.0}, 0.1)[0] for _ in range(20)]       # dt = 100 tau
    assert all(t <= 35.0 + 1e-9 for t in temps)
    assert temps[-1] == pytest.approx(35.0, rel=1e-6)
    assert all(b >= a - 1e-12 for a, b in zip(temps, temps[1:]))       # no oscillation


def test_single_node_transient_matches_exponential():
    r, c = 1.0, 1.0
    net = ThermalRC(["n"], [c], [("n", "ambient", r)], 0.0)
    for _ in range(1000):
        net.step({"n": 1.0}, 1e-3)
    assert net.temp("n") == pytest.approx(1 - math.exp(-1.0), rel=2e-3)


def test_soc_network_steady_state_consistent():
    soc, params = load_soc(), load_params()
    doms = ("cpu_a", "cpu_b", "rt", "npu", "simt", "dsp", "noc_cache", "dram_io")
    net = build_soc_thermal(soc, params, doms, 25.0)
    p = {d: 1.0 for d in doms}
    ss = net.steady_state(p)
    th = params["thermal"]
    total = 8.0
    t_enc = 25 + total * th["enclosure_to_ambient_r_k_per_w"]
    t_pkg = t_enc + total * th["package_to_enclosure_r_k_per_w"]
    assert ss[net.idx["enclosure"]] == pytest.approx(t_enc)
    assert ss[net.idx["package"]] == pytest.approx(t_pkg)
    assert all(ss[net.idx[d]] > t_pkg for d in doms)


def test_throttle_hysteresis():
    ctl = ThrottleController(max_index=7, throttle_c=100, release_c=95, trip_c=105, step_ps=10 * MS)
    t = 0
    seq = []
    for temp in [90, 101, 101, 101, 98, 98, 94, 94, 94, 106, 97, 90]:
        t += 10 * MS
        seq.append(ctl.update(temp, t))
    # steps down one OPP per 10 ms above 100 C, holds in the 95-100 band, steps up below 95,
    # drops to the lowest OPP at the 105 C trip, holds in band, then recovers one step
    assert seq == [7, 6, 5, 4, 4, 4, 5, 6, 7, 0, 0, 1]


def test_throttle_respects_step_interval():
    ctl = ThrottleController(max_index=5, throttle_c=100, release_c=95, trip_c=105, step_ps=10 * MS)
    caps = [ctl.update(102, k * MS) for k in range(1, 25)]
    assert caps.count(4) == 10 and caps.count(3) == 10 and min(caps) == 2   # one step per 10 ms


def test_regulator_curve_shape():
    reg = RegulatorModel(rated_w=10.0, a0=0.01, a1=0.06, a2=0.03)
    effs = [reg.efficiency(p) for p in (0.05, 0.5, 5.0, 10.0, 15.0)]
    assert effs[0] < effs[1] < effs[2]
    assert effs[4] < effs[2]
    assert 0.85 < reg.max_efficiency() < 0.95
    assert reg.input_w(5.0) == pytest.approx(5.0 + 10.0 * (0.01 + 0.06 * 0.5 + 0.03 * 0.25))


def test_transition_energy():
    assert transition_energy_j(200e-9, 0.6, 0.8) == pytest.approx(0.5 * 200e-9 * (0.64 - 0.36))
    assert transition_energy_j(200e-9, 0.8, 0.6) == transition_energy_j(200e-9, 0.6, 0.8)
