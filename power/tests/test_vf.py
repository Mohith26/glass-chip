import math

import pytest

from power.params import load_soc
from power.vf import VFCurve, build_domain_vf, extend_below, nominal_frequency

SOC = load_soc()
VF = build_domain_vf(SOC)


@pytest.mark.parametrize("domain", [d for d in VF if not VF[d].fixed])
def test_f_at_v_nominal_equals_block_nominal(domain):
    dvf = VF[domain]
    v_nom = SOC["process"]["v_nominal"]
    assert dvf.curve.freq(v_nom) == pytest.approx(nominal_frequency(SOC, domain), rel=1e-12)
    assert dvf.nominal.volts == pytest.approx(v_nom)
    assert dvf.nominal.freq_hz == pytest.approx(nominal_frequency(SOC, domain), rel=1e-12)


def test_opp_tables_match_config():
    for d, dvf in VF.items():
        assert [o.volts for o in dvf.opps] == sorted(SOC["power_domains"][d]["opp_volts"])
    assert VF["dram_io"].fixed and len(VF["dram_io"].opps) == 1


def test_frequency_monotonic_and_inverse():
    c = VF["npu"].curve
    vs = [0.35 + 0.01 * i for i in range(60)]
    fs = [c.freq(v) for v in vs]
    assert all(b > a for a, b in zip(fs, fs[1:]))
    for v in (0.45, 0.6, 0.75, 0.8):
        assert c.min_voltage_for(c.freq(v)) == pytest.approx(v, abs=1e-6)
    assert c.freq(c.vth) == 0.0


def test_alpha_power_law_formula():
    c = VFCurve(1e9, 0.75, 0.30, 1.3)
    v = 0.6
    assert c.freq(v) == pytest.approx(c.k * (v - 0.30) ** 1.3 / v)
    assert c.freq(0.75) == pytest.approx(1e9)


def test_lowest_index_for_freq():
    dvf = VF["cpu_a"]
    f_mid = dvf.opps[3].freq_hz
    assert dvf.lowest_index_for_freq(f_mid) == 3
    assert dvf.lowest_index_for_freq(f_mid * 1.0001) == 4
    assert dvf.lowest_index_for_freq(math.inf) == dvf.max_index


def test_extend_below_marks_extrapolated():
    ext = extend_below(VF["npu"], 0.40)
    assert ext.opps[0].volts == pytest.approx(0.40) and ext.opps[0].extrapolated
    assert not any(o.extrapolated for o in ext.opps[1:])
    assert ext.nominal.volts == pytest.approx(0.75)
