"""Tests for the RISC-V study pieces that run without gem5."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from riscv.core_hook import load_table, t5_variant  # noqa: E402

HOST = ROOT / "riscv/build/kbench_host"


@pytest.mark.skipif(not HOST.exists(), reason="run make -C riscv/kernels first")
def test_host_kernels_agree_across_variants() -> None:
    """On the host every variant name runs the C reference, so all variants must agree bit for bit,
    and the polynomial FK must stay within 1 um of a double-precision libm FK."""
    outs = {}
    for v in ("scalar", "rvv", "xcol"):
        p = subprocess.run([str(HOST), "esdf", v, "1", "dump"], capture_output=True, text=True, check=True)
        outs[v] = [l for l in p.stdout.splitlines() if l.startswith("D ")]
    assert outs["scalar"] == outs["rvv"] == outs["xcol"]
    assert len(outs["scalar"]) == 128 * 61
    fk = subprocess.run([str(HOST), "fk", "scalar", "1"], capture_output=True, text=True, check=True).stdout
    err = float(fk.split("max_abs_err_vs_libm_double_m=")[1].split()[0])
    assert err < 1e-6


def test_table_verified() -> None:
    t = load_table()
    assert t["verification"] and all(v["ok"] for v in t["verification"].values())
    for k in ("esdf_scalar", "esdf_rvv", "esdf_xcol", "selfcol_rvv"):
        assert t["kernels"][k]["o3"]["cpi"] > 0


@pytest.mark.parametrize("variant", ["scalar", "rvv", "xcol"])
def test_t5_variant_is_a_valid_dag(variant: str) -> None:
    base = json.loads((ROOT / "workloads/graphs/T5_plan.json").read_text())
    g, cpi, summ = t5_variant(base, load_table(), variant)
    ids = [o["op_id"] for o in g["ops"]]
    assert len(ids) == len(set(ids))
    seen: set[str] = set()
    for o in g["ops"]:
        for d in o.get("depends_on", []):
            assert d in seen, f"{o['op_id']} depends on {d} before it is defined"
        seen.add(o["op_id"])
    assert not any(i.endswith(".collision") for i in ids)
    assert set(cpi) == {i for i in ids if ".collision." in i}
    assert len(summ["blocks"]) == 5 and summ["blocks"][0]["self_pairs"] == summ["blocks"][0]["states"] * 694
