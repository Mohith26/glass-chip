"""Dynamic energy per operation class: E = activity * C_eff * V^2.

C_eff for each class is derived from a published per-op energy seed (see
power/params_vx1.yaml and power/README.md for the source of each seed):

    E_5nm(V_ref)  = E_seed * node_scaling[kind]
    C_eff         = E_5nm(V_ref) / V_ref^2
    E(V)          = activity * C_eff * V^2

V_ref is `seed_voltage_v` (0.75 V) for on-die classes, and the dram_io domain
voltage for DRAM classes (DRAM device and PHY energy do not follow SoC DVFS).
All values are assumptions, not measurements of any chip.

Also here: clock-tree (always-on switching) power, estimated peak dynamic power per
domain, and the hypothetical low-voltage arithmetic scaling for the NPU MAC array.
"""
from __future__ import annotations

from typing import Any, Mapping

from .params import DOMAINS
from .vf import DomainVF

MAC_CLASSES = ("int8_macs", "bf16_macs", "fp8_macs")
DRAM_CLASSES = ("dram_bytes", "dram_activates")
ACTIVITY_CLASSES = (
    "int8_macs", "bf16_macs", "fp8_macs", "sram_bytes", "flit_hops", "cache_bytes",
    "dram_bytes", "dram_activates", "cpu_instr", "vec_ops", "simt_flops", "dsp_ops", "dma_bytes",
)


class EnergyModel:
    """Per-class switching energy and per-domain clock-tree power."""

    def __init__(self, params: Mapping[str, Any], soc: Mapping[str, Any], vf: Mapping[str, DomainVF]):
        self.params = params
        self.soc = soc
        self.vf = vf
        self.v_nominal = float(soc["process"]["v_nominal"])
        self.v_seed = float(params.get("seed_voltage_v", self.v_nominal))
        dram_dom = params.get("dram_reference_domain", "dram_io")
        self.v_dram_ref = float(soc["power_domains"][dram_dom]["opp_volts"][0])
        scaling = params["node_scaling"]
        act = params.get("activity_factor", {})
        self.activity = {c: float(act.get(c, act.get("default", 1.0))) for c in ACTIVITY_CLASSES}
        # C_eff in farads (J / V^2) per operation, by class.
        self.c_eff: dict[str, float] = {}
        self.seed_src: dict[str, str] = {}
        for cls, spec in params["energy_seeds_pj"].items():
            e5 = float(spec["pj"]) * 1e-12 * float(scaling[spec["kind"]])
            v_ref = self.v_dram_ref if cls in DRAM_CLASSES else self.v_seed
            self.c_eff[cls] = e5 / v_ref ** 2
            self.seed_src[cls] = str(spec.get("src", ""))
        self.domain_scale: dict[str, dict[str, float]] = {
            d: {k: float(v) for k, v in m.items()} for d, m in (params.get("domain_class_scale") or {}).items()
        }
        lv = params.get("low_voltage_arith", {}) or {}
        self.lv_enabled = bool(lv.get("enabled", False))
        self.lv_domain = str(lv.get("domain", "npu"))
        self.lv_reduction_x = float(lv.get("reduction_x", 1.0)) if self.lv_enabled else 1.0
        if self.lv_reduction_x <= 0:
            raise ValueError("low_voltage_arith.reduction_x must be > 0")
        self.peak_activity = {d: {k: float(v) for k, v in m.items()}
                              for d, m in params.get("peak_activity_per_cycle", {}).items()}
        ctf = params.get("clock_tree_fraction", {})
        self.clock_fraction = {d: float(ctf.get(d, ctf.get("default", 0.1))) for d in DOMAINS}
        self._peak_nom = {d: self.peak_dynamic_w(d) for d in vf}

    # ------------------------------------------------------------------ per op
    def op_energy_j(self, cls: str, v: float, domain: str | None = None) -> float:
        """Energy of one operation of class `cls` at supply voltage v in `domain`."""
        c = self.c_eff[cls] * self.activity.get(cls, 1.0)
        if domain is not None:
            c *= self.domain_scale.get(domain, {}).get(cls, 1.0)
            if domain == self.lv_domain and cls in MAC_CLASSES:
                c /= self.lv_reduction_x
        if cls in DRAM_CLASSES:
            v = self.v_dram_ref
        return c * v * v

    def voltages_for(self, domain: str, v: float) -> tuple[float, float]:
        """(MAC-array voltage, rest-of-domain voltage).

        With the low-voltage arithmetic toggle on, only the NPU MAC array may run below the
        configured table v_min; SRAM and control stay at max(v, table v_min). Otherwise both
        equal v.
        """
        if self.lv_enabled and domain == self.lv_domain:
            return v, max(v, self.vf[domain].table_v_min)
        return v, v

    def dynamic_energy_j(self, domain: str, counts: Mapping[str, Any], v: float) -> tuple[float, dict[str, float]]:
        """Switching energy for activity counts executed at voltage v. Returns (total, by class)."""
        v_mac, v_rest = self.voltages_for(domain, v)
        by_cls: dict[str, float] = {}
        total = 0.0
        for cls in ACTIVITY_CLASSES:
            n = counts.get(cls)
            if not n:
                continue
            vv = v_mac if cls in MAC_CLASSES else v_rest
            e = float(n) * self.op_energy_j(cls, vv, domain)
            by_cls[cls] = e
            total += e
        return total, by_cls

    # --------------------------------------------------------------- per domain
    def energy_per_busy_cycle_j(self, domain: str, v: float, utilization: float = 1.0) -> float:
        """Switching energy of one busy cycle at peak activity (times utilization)."""
        counts = {k: n * utilization for k, n in self.peak_activity.get(domain, {}).items()}
        return self.dynamic_energy_j(domain, counts, v)[0]

    def peak_dynamic_w(self, domain: str, v: float | None = None, f_hz: float | None = None) -> float:
        """Estimated switching power at 100 percent activity (assumption-driven estimate)."""
        dvf = self.vf[domain]
        v = dvf.nominal.volts if v is None else v
        f = dvf.nominal.freq_hz if f_hz is None else f_hz
        return self.energy_per_busy_cycle_j(domain, v) * f

    def peak_dynamic_nominal_w(self, domain: str) -> float:
        return self._peak_nom[domain]

    def clock_power_w(self, domain: str, v: float, f_hz: float) -> float:
        """Clock-tree and always-on switching while clocked: frac * P_peak_nom * (V/Vn)^2 * f/fn."""
        dvf = self.vf[domain]
        _, v_rest = self.voltages_for(domain, v)
        vn, fn = dvf.nominal.volts, dvf.nominal.freq_hz
        return self.clock_fraction[domain] * self._peak_nom[domain] * (v_rest / vn) ** 2 * (f_hz / fn)

    @property
    def hypothetical(self) -> bool:
        """True when the low-voltage arithmetic factor is not 1x (Velaura-claim territory)."""
        return self.lv_enabled and self.lv_reduction_x != 1.0
