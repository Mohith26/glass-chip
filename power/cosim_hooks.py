"""Glue between the co-sim bridge (cosim/bridge.py) and the PowerManager.

The bridge calls `power_manager.step(chip, t_ps)` every physics step, but it consumes job
completions itself (`_drain`). Two small adapters let the manager see what it needs without
changing the bridge:

* `ObservedChip(chip, pm)`: forwards everything to the chip; `run_until` also passes the
  completions to `pm.observe_completions` (P4/P6 statistics, miss counts, P5 job trace).
* `make_event_hook(pm, chained)`: handles scenario events `set_ambient` and `set_vmin`
  (S5 and S8), then calls the chained hook if any.
"""
from __future__ import annotations

from typing import Any, Callable

EventHook = Callable[[dict[str, Any], Any], None]


class ObservedChip:
    """Chip proxy that reports completions to a PowerManager."""

    def __init__(self, chip: Any, pm: Any):
        self._chip = chip
        self._pm = pm

    @property
    def inner(self) -> Any:
        return self._chip

    def run_until(self, t_ps: int) -> list[dict[str, Any]]:
        out = self._chip.run_until(t_ps)
        if out:
            self._pm.observe_completions(out)
        return out

    def __getattr__(self, item: str) -> Any:
        return getattr(self._chip, item)


def make_event_hook(pm: Any, chained: EventHook | None = None) -> EventHook:
    """Event hook for CoSim(event_hook=...): routes set_ambient / set_vmin to the manager."""

    def hook(event: dict[str, Any], chip: Any) -> None:
        action = event.get("action")
        args = event.get("args") or {}
        if action == "set_ambient":
            pm.set_ambient(float(args.get("ambient_c", args.get("c", 25.0))))
        elif action == "set_vmin":
            v = args.get("v_min", args.get("v", args.get("volts")))
            if v is not None and args.get("domain"):
                pm.set_vmin(str(args["domain"]), float(v))
        if chained is not None:
            chained(event, chip)

    return hook
