import pytest

from power.energy import ACTIVITY_CLASSES, DRAM_CLASSES, EnergyModel
from power.leakage import LeakageModel
from power.params import load_params, load_soc
from power.vf import build_domain_vf

SOC = load_soc()
PARAMS = load_params()
VF = build_domain_vf(SOC)
EM = EnergyModel(PARAMS, SOC, VF)


@pytest.mark.parametrize("cls", [c for c in ACTIVITY_CLASSES if c not in DRAM_CLASSES])
def test_dynamic_energy_scales_with_v_squared(cls):
    counts = {cls: 1000.0}
    e1, _ = EM.dynamic_energy_j("cpu_a", counts, 0.5)
    e2, _ = EM.dynamic_energy_j("cpu_a", counts, 0.8)
    assert e1 > 0
    assert e2 / e1 == pytest.approx((0.8 / 0.5) ** 2, rel=1e-12)


def test_dram_energy_does_not_follow_soc_voltage():
    counts = {"dram_bytes": 1e6, "dram_activates": 100}
    assert EM.dynamic_energy_j("dram_io", counts, 0.5)[0] == pytest.approx(EM.dynamic_energy_j("dram_io", counts, 0.9)[0])


def test_seed_values_reproduced_at_seed_voltage():
    # int8 MAC: 0.10 pJ (Jouppi 2021, 7 nm) x 0.8 logic scaling at 0.75 V
    assert EM.op_energy_j("int8_macs", 0.75) == pytest.approx(0.10e-12 * 0.8)
    assert EM.op_energy_j("dram_bytes", 0.5) == pytest.approx(36e-12)
    # rt lockstep doubles instruction energy
    assert EM.op_energy_j("cpu_instr", 0.75, "rt") == pytest.approx(2 * EM.op_energy_j("cpu_instr", 0.75))


def test_energy_linear_in_activity():
    a = EM.dynamic_energy_j("npu", {"int8_macs": 1e6, "sram_bytes": 1e4}, 0.7)[0]
    b = EM.dynamic_energy_j("npu", {"int8_macs": 2e6, "sram_bytes": 2e4}, 0.7)[0]
    assert b == pytest.approx(2 * a)


def test_low_voltage_arith_toggle_default_is_1x_and_not_hypothetical():
    assert not EM.hypothetical
    p = dict(PARAMS, low_voltage_arith=dict(PARAMS["low_voltage_arith"], enabled=True, reduction_x=1.0))
    assert not EnergyModel(p, SOC, VF).hypothetical


def test_low_voltage_arith_scales_only_mac_energy_and_is_labeled():
    p = dict(PARAMS, low_voltage_arith=dict(PARAMS["low_voltage_arith"], enabled=True, reduction_x=3.0))
    em = EnergyModel(p, SOC, VF)
    assert em.hypothetical
    assert em.op_energy_j("int8_macs", 0.7, "npu") == pytest.approx(EM.op_energy_j("int8_macs", 0.7, "npu") / 3.0)
    assert em.op_energy_j("sram_bytes", 0.7, "npu") == pytest.approx(EM.op_energy_j("sram_bytes", 0.7, "npu"))
    assert em.op_energy_j("int8_macs", 0.7, "cpu_a") == pytest.approx(EM.op_energy_j("int8_macs", 0.7, "cpu_a"))
    # below the table v_min the MAC array scales but SRAM stays at the table v_min
    v_mac, v_rest = em.voltages_for("npu", 0.40)
    assert v_mac == 0.40 and v_rest == pytest.approx(VF["npu"].table_v_min)


def test_clock_power_scales_v2f():
    d = "simt"
    n = VF[d].nominal
    p_nom = EM.clock_power_w(d, n.volts, n.freq_hz)
    assert p_nom == pytest.approx(EM.clock_fraction[d] * EM.peak_dynamic_nominal_w(d))
    assert EM.clock_power_w(d, n.volts / 2, n.freq_hz / 2) == pytest.approx(p_nom / 8)


def test_leakage_rises_with_temperature_and_v():
    peak = {d: EM.peak_dynamic_nominal_w(d) for d in VF}
    lk = LeakageModel.from_params(PARAMS, peak, {d: VF[d].nominal.volts for d in VF})
    temps = [25, 45, 65, 85, 105]
    vals = [lk.power_w("npu", 0.75, t) for t in temps]
    assert all(b > a for a, b in zip(vals, vals[1:]))
    c_t = PARAMS["leakage"]["c_t_per_c"]
    assert vals[-1] / vals[0] == pytest.approx(pytest.importorskip("math").exp(c_t * 80))
    assert lk.power_w("npu", 0.75, 25) == pytest.approx(PARAMS["leakage"]["fraction_of_peak_dynamic"] * peak["npu"])
    assert lk.power_w("npu", 0.6, 50) < lk.power_w("npu", 0.75, 50)
