"""Power policies P0-P6. `make_policy(cfg)` builds one from a configs/policies/*.yaml dict."""
from __future__ import annotations

from typing import Any, Mapping

from .base import PolicyContext, PowerPolicy
from .pace import PaceToDeadlinePolicy, SurrogateFn, SurrogateGuidedPolicy
from .static import NominalPolicy, OfflineOptimalOnline, RaceToIdlePolicy

POWER_MODES = {
    "nominal": NominalPolicy,
    "race_to_idle": RaceToIdlePolicy,
    "pace_to_deadline": PaceToDeadlinePolicy,
    "surrogate_guided": SurrogateGuidedPolicy,
    "offline_optimal": OfflineOptimalOnline,
}


def make_policy(cfg: Mapping[str, Any], surrogate: SurrogateFn | None = None) -> PowerPolicy:
    """Instantiate the power policy named by cfg["power"]."""
    mode = str(cfg.get("power", "nominal"))
    if mode not in POWER_MODES:
        raise ValueError(f"unknown power mode {mode!r}; expected one of {sorted(POWER_MODES)}")
    params = cfg.get("params") or {}
    if mode == "surrogate_guided":
        return SurrogateGuidedPolicy(params, surrogate=surrogate)
    return POWER_MODES[mode](params)


__all__ = [
    "PolicyContext", "PowerPolicy", "NominalPolicy", "RaceToIdlePolicy", "PaceToDeadlinePolicy",
    "SurrogateGuidedPolicy", "OfflineOptimalOnline", "make_policy", "SurrogateFn", "POWER_MODES",
]
