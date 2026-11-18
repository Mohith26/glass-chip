"""Provenance helpers for the power studies (s06-s08)."""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from typing import Any, Iterable

from .params import ROOT

SURROGATE_BUNDLE = ROOT / "data" / "surrogate" / "bundle.pkl"


def surrogate_version(path: Path = SURROGATE_BUNDLE) -> dict[str, Any] | None:
    """Identify the surrogate bundle P6 loads: sha256 prefix and mtime of data/surrogate/bundle.pkl."""
    if not path.exists():
        return None
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return {"file": str(path.relative_to(ROOT)), "sha256_12": h,
            "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")}


def check_build_ids(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collect the core build ids recorded by every run; flag if they are not all the same."""
    ids = sorted({str((r.get("provenance") or {}).get("core_build", {}).get("build_id")) for r in rows})
    return {"build_ids": ids, "single_build": len(ids) == 1 and ids[0] != "None"}
