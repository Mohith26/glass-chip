"""Runs the PowerManager against the real glasschip_core if it has been built (skipped otherwise)."""
import glob
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "core" / "build"
SO = glob.glob(str(BUILD / "glasschip_core*.so"))

pytestmark = pytest.mark.skipif(not SO, reason="glasschip_core not built yet")


def test_manager_drives_real_core():
    sys.path.insert(0, str(BUILD))
    import glasschip_core as gc

    from power.manager import PowerManager
    from power.params import load_policy, load_soc, load_yaml

    soc = load_soc()
    fid_files = sorted((ROOT / "configs" / "fidelity").glob("*.yaml"))
    fidelity = load_yaml(fid_files[0]) if fid_files else {"cpu": "L0", "npu": "L0", "simt": "L0", "dsp": "L0",
                                                          "noc": "L0", "dram": "L0", "cache": "L0", "quantum_ns": 100}
    sim = gc.Sim(soc, fidelity, 1)
    graphs = [json.loads(p.read_text()) for p in sorted((ROOT / "workloads" / "graphs").glob("T*.json"))]
    for g in graphs:
        sim.add_task(g)
    pm = PowerManager(load_policy("P4"), soc, 25.0)
    pm.apply_scheduler(sim)
    ms = 1_000_000_000
    for k in range(30):
        t = k * ms
        for g in graphs:
            period = (g.get("release") or {}).get("period_us")
            if period and (t % int(period * 1e6)) < ms:
                sim.release_job(g["task_id"], t, k)
        pm.step(sim, t)
        pm.observe_completions(sim.run_until(t + ms))
    pm.step(sim, 30 * ms)
    s = pm.summary()
    assert s["energy_j"] > 0
    for d, dvf in pm.vf.items():
        if not dvf.fixed:
            assert sim.domain_freq(d) == pytest.approx(dvf.opps[pm.opp[d]].freq_hz, rel=1e-6)
