#!/usr/bin/env python3
"""Numeric cross-check for public docs.

Every number that sits next to a known metric phrase in a checked document must be
backed by a field in a results JSON. A number is backed when a source comment of the
form

    <!-- src: results/s06.json#path.to.field -->            (Markdown / HTML)
    % src: results/s06.json#path.to.field                    (LaTeX)

appears on the same line or within the two lines above it, and the referenced value,
after rounding to the precision written in the doc, equals the written number.
Paths use dots for keys and [i] for list indices, e.g. `policies.P4.energy_wh_per_robot_hour`.
Keys that contain spaces, dots or other punctuation are written in brackets with double quotes,
e.g. `headline.t8_misses["recording 24 Gbit/s | P0 FIFO"]`; plain keys may also contain `+`, `@`
and `:` (e.g. `analysis.scenarios.L1+cpu@L2`, `speed.A:L1`).
Optional modifiers after the path: `|pct` (value is a fraction, doc shows percent),
`|x` (doc shows a ratio like "2.3x"), `|scale=1e-3` (multiply before comparing).

Accepted source files are committed JSON written by committed scripts: `results/*.json` (the
studies) and, for component docs that describe calibration and benchmark runs, the tool and bench
tables `core/bench/*.json`, `calib/tables/*.json` and `riscv/tables/*.json`. Public top-level docs
(README, report, one-pager) cite `results/` only.

Lines that are not checked:
- lines marked TEMPLATE (placeholders before runs exist);
- lines containing `numcheck: ignore` (for numbers that are not results: config inputs, format
  constants, citations of published figures, units in format specs). Use it per line, never for
  a line that states a result;
- headings, table separator rows and fenced code blocks.

Within a checked line, these are not treated as result numbers (false-positive handling):
- text inside inline code spans (`...`), which holds identifiers, file names, commands and keys;
- numbers that are part of a hyphenated name such as ResNet-50 or LPDDR5-6400 (a word that
  contains a letter, then `-`, then the number); ranges such as 0.1-0.4 are still checked;
- the first factor of a dimension written NxM such as 64x64 or 4x4 (the second factor is never
  matched because it follows a letter);
- single-digit integers without % or x.

Usage: python tools/numcheck.py [files...]   (defaults to DEFAULT_FILES below)
Exit code 1 if any metric number lacks a source or mismatches.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

METRIC_PHRASES = [
    r"energy per robot[- ]hour", r"deadline miss", r"miss rate", r"\bp50\b", r"\bp95\b", r"\bp99\b",
    r"x faster", r"% lower", r"% higher", r"% less", r"% more", r"speedup", r"coverage",
    r"latency", r"success rate", r"bandwidth", r"\bW\b", r"\bmW\b", r"\bJ\b", r"\bWh\b", r"error",
    r"throughput", r"utilization", r"temperature", r"\bGB/s\b", r"\bms\b", r"\bus\b",
]
METRIC_RE = re.compile("|".join(METRIC_PHRASES), re.IGNORECASE)
NUM_RE = re.compile(r"(?<![\w.])(-?\d+(?:,\d{3})*(?:\.\d+)?)(\s*%|x\b)?")
SRC_RE = re.compile(r"(?:<!--|%)\s*src:\s*((?:results|core/bench|calib/tables|riscv/tables)/[\w./-]+\.json)"
                    r"#((?:\[\"[^\"]*\"\]|\[\d+\]|[\w.+@:-])+)((?:\|[\w=.+-]+)*)")
CODE_SPAN_RE = re.compile(r"`[^`]*`")
DEFAULT_FILES = (
    "README.md", "report/*.md", "report/*.tex", "docs/*.md",
    "core/README.md", "configs/fidelity/README.md", "workloads/README.md", "cosim/README.md",
    "power/README.md", "calib/README.md", "calib/VALIDATION.md", "surrogate/README.md",
    "riscv/README.md", "viz/README.md",
)
# Numbers that are structural, not results (section numbers, years, list markers, config inputs)
IGNORE_LINE_RE = re.compile(r"TEMPLATE|^\s*#|^\s*\|?\s*-{3,}|```|numcheck:\s*ignore", re.IGNORECASE)


def resolve(obj, path: str):
    for part in re.findall(r'\["[^"]*"\]|\[\d+\]|[^.\[\]]+', path):
        if part.startswith('["'):
            obj = obj[part[2:-2]]
        elif part.startswith("["):
            obj = obj[int(part[1:-1])]
        else:
            obj = obj[part]
    return obj


def decimals(s: str) -> int:
    return len(s.split(".")[1]) if "." in s else 0


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_code = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or IGNORE_LINE_RE.search(line) or not METRIC_RE.search(line):
            continue
        text = CODE_SPAN_RE.sub("", SRC_RE.sub("", line))
        nums = [m for m in NUM_RE.finditer(text)]
        if not nums:
            continue
        window = "\n".join(lines[max(0, i - 2): i + 1])
        srcs = SRC_RE.findall(window)
        values = []
        for f, p, mods in srcs:
            try:
                data = json.loads((ROOT / f).read_text())
                v = resolve(data, p)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{path}:{i+1}: bad source {f}#{p}: {e}")
                continue
            scale = 1.0
            for mod in mods.split("|")[1:]:
                if mod == "pct":
                    scale *= 100.0
                elif mod.startswith("scale="):
                    scale *= float(mod.split("=", 1)[1])
            if isinstance(v, (int, float)):
                values.append(float(v) * scale)
        for m in nums:
            raw = m.group(1).replace(",", "")
            start, end = m.start(1), m.end(1)
            # hyphenated names (ResNet-50, LPDDR5-6400): a word containing a letter, '-', the number
            if re.search(r"[A-Za-z]\w*-$", text[:start]):
                continue
            # first factor of a dimension such as 64x64
            if re.match(r"\s*x\d", text[end:]):
                continue
            # skip tiny structural integers (1-9) unless followed by % or x
            if not m.group(2) and re.fullmatch(r"\d", raw):
                continue
            written = float(raw)
            d = decimals(raw)
            ok = any(round(v, d) == round(written, d) or abs(v - written) <= 0.5 * 10 ** (-d) + 1e-12 for v in values)
            if not ok:
                errors.append(f"{path}:{i+1}: number {m.group(0).strip()} near a metric phrase has no matching source"
                              + (f" (sources give {values})" if values else " (no src comment)"))
    return errors


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or [p for pat in DEFAULT_FILES for p in sorted(ROOT.glob(pat))]
    errors: list[str] = []
    for f in files:
        errors += check_file(f if f.is_absolute() else ROOT / f)
    for e in errors:
        print(e)
    print(f"numcheck: {len(files)} files, {len(errors)} problems")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
