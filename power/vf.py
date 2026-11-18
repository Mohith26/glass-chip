"""Voltage-frequency model and operating-performance-point (OPP) tables.

Equation (alpha-power law, Sakurai and Newton, IEEE JSSC 1990):

    f(V) = k * (V - Vth)^alpha / V

k is fitted per domain so that f(v_nominal) equals the block's nominal frequency
from configs/soc/vx1.yaml. Vth, alpha and v_nominal come from the `process`
section of the SoC config (all assumptions for a generic 5 nm class node).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .params import DOMAINS, domain_blocks


@dataclass(frozen=True)
class VFCurve:
    """Alpha-power-law frequency as a function of supply voltage."""

    f_nominal_hz: float
    v_nominal: float
    vth: float
    alpha: float

    @property
    def k(self) -> float:
        return self.f_nominal_hz * self.v_nominal / (self.v_nominal - self.vth) ** self.alpha

    def freq(self, v: float) -> float:
        """Maximum frequency at voltage v (0 at or below Vth)."""
        if v <= self.vth:
            return 0.0
        return self.k * (v - self.vth) ** self.alpha / v

    def min_voltage_for(self, f_hz: float, v_hi: float = 2.0, tol: float = 1e-7) -> float:
        """Smallest voltage that supports frequency f_hz (inverse of freq, by bisection).

        f(V) is monotonically increasing for V > Vth when alpha > 1, so bisection is exact
        up to `tol`.
        """
        if f_hz <= 0:
            return self.vth
        if self.freq(v_hi) < f_hz:
            raise ValueError(f"{f_hz:.3g} Hz is above f({v_hi} V)")
        lo, hi = self.vth, v_hi
        while hi - lo > tol:
            mid = 0.5 * (lo + hi)
            if self.freq(mid) >= f_hz:
                hi = mid
            else:
                lo = mid
        return hi


@dataclass(frozen=True)
class OPP:
    """One operating point: supply voltage and the frequency it supports."""

    index: int
    volts: float
    freq_hz: float
    extrapolated: bool = False   # True if below the SoC config's table (set_vmin or low-voltage toggle)


@dataclass
class DomainVF:
    """V/f description of one power domain."""

    name: str
    curve: VFCurve | None          # None for fixed-frequency domains (dram_io)
    opps: list[OPP]
    nominal_index: int
    table_v_min: float
    transition_ps: int
    fixed_freq_hz: float | None = None
    floor_index: int = 0           # lowest OPP the policies may use (set_vmin)
    pinned_index: int | None = None  # static undervolt pin (set_vmin on nominal policies)
    notes: list[str] = field(default_factory=list)

    @property
    def fixed(self) -> bool:
        return self.curve is None

    @property
    def max_index(self) -> int:
        return len(self.opps) - 1

    @property
    def nominal(self) -> OPP:
        return self.opps[self.nominal_index]

    def lowest_index_for_freq(self, f_hz: float) -> int:
        """Lowest OPP (at or above the floor) whose frequency is >= f_hz; max OPP if none."""
        for o in self.opps[self.floor_index:]:
            if o.freq_hz >= f_hz * (1 - 1e-12):
                return o.index
        return self.max_index

    def index_at_or_below_voltage(self, v: float) -> int:
        """Highest OPP with voltage <= v (0 if none)."""
        best = 0
        for o in self.opps:
            if o.volts <= v + 1e-9:
                best = o.index
        return best


def nominal_frequency(soc: dict[str, Any], domain: str) -> float | None:
    """Nominal frequency of a domain: max freq_hz among its blocks (NoC counts for noc_cache)."""
    freqs = [float(b["freq_hz"]) for b in domain_blocks(soc, domain) if "freq_hz" in b]
    return max(freqs) if freqs else None


def build_domain_vf(soc: dict[str, Any], domains: tuple[str, ...] = DOMAINS) -> dict[str, DomainVF]:
    """Build OPP tables for every power domain from `power_domains.opp_volts`.

    Domains without a block frequency (dram_io) are fixed-frequency: the PHY rate does
    not follow the SoC V/f curve, so the policies never change it.
    """
    proc = soc["process"]
    v_nom, vth, alpha = float(proc["v_nominal"]), float(proc["vth"]), float(proc["alpha"])
    pd = soc["power_domains"]
    trans_ps = int(round(float(pd.get("dvfs_transition_us", 20)) * 1e6))
    out: dict[str, DomainVF] = {}
    for d in domains:
        spec = pd[d]
        volts = sorted(float(v) for v in spec["opp_volts"])
        f_nom = nominal_frequency(soc, d)
        if f_nom is None or len(volts) == 1:
            dram = soc.get("blocks", {}).get("dram", {})
            f_fixed = f_nom if f_nom is not None else float(dram.get("data_rate_mts", 0)) * 1e6
            opps = [OPP(0, volts[0], f_fixed)]
            out[d] = DomainVF(d, None, opps, 0, volts[0], trans_ps, fixed_freq_hz=f_fixed)
            continue
        curve = VFCurve(f_nom, v_nom, vth, alpha)
        opps = [OPP(i, v, curve.freq(v)) for i, v in enumerate(volts)]
        nominal_index = min(range(len(volts)), key=lambda i: abs(volts[i] - v_nom))
        out[d] = DomainVF(d, curve, opps, nominal_index, float(spec.get("v_min", volts[0])), trans_ps)
    return out


def extend_below(dvf: DomainVF, v_new_min: float, step_v: float = 0.05, label: str = "extrapolated") -> DomainVF:
    """Add OPPs below the table minimum down to v_new_min (marked extrapolated)."""
    if dvf.fixed or dvf.curve is None:
        return dvf
    lowest = dvf.opps[0].volts
    if v_new_min >= lowest - 1e-9:
        return dvf
    new_volts: list[float] = []
    v = lowest - step_v
    while v > v_new_min + 1e-9:
        new_volts.append(round(v, 6))
        v -= step_v
    new_volts.append(round(v_new_min, 6))
    volts = sorted(set(new_volts)) + [o.volts for o in dvf.opps]
    extra = len(volts) - len(dvf.opps)
    opps = []
    for i, v in enumerate(volts):
        opps.append(OPP(i, v, dvf.curve.freq(v), extrapolated=i < extra))
    out = replace(dvf, opps=opps, nominal_index=dvf.nominal_index + extra,
                  floor_index=0, pinned_index=None, notes=dvf.notes + [f"{label}: OPPs added below {lowest} V"])
    return out
