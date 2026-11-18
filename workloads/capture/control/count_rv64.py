"""Cross-compile the control/estimation/SSM kernels to RV64 assembly with clang and
count instructions per call statically.

Method: compile with `clang --target=riscv64-linux-gnu -O2 -S -ffreestanding`,
take each exported function's body, find loops as backward branches, weight
every instruction by the trip counts of the loops that contain it (trip counts
are known constants passed in below), inline the counts of local callees, and
classify each instruction as scalar, vector (RVV) or memory (loads/stores).
Forward branches are not resolved, so both sides of an if/else are counted:
the result is exact for straight-line code and an upper bound otherwise.

Run: .venv/bin/python workloads/capture/control/count_rv64.py
Writes workloads/capture/control/rv64_counts.json and the .s files under data/workloads/rv64/.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ASM_DIR = ROOT / "data" / "workloads" / "rv64"

# function -> (source, -march, loop trip counts in order of the backward branches)
TARGETS = {
    "control_step": ("control.cpp", "rv64gc", []),
    "rnea_gravity_coriolis": ("control.cpp", "rv64gc", []),
    "estimate_step": ("estimate.cpp", "rv64gcv", []),
    "ssm_step": ("ssm.cpp", "rv64gc", [64]),
}

LOADS = {"lb", "lh", "lw", "ld", "lbu", "lhu", "lwu", "flh", "flw", "fld"}
STORES = {"sb", "sh", "sw", "sd", "fsh", "fsw", "fsd"}
VMEM = re.compile(r"^v(l|s)(e\d|se\d|uxei|oxei|\d+re?\d*|m\.v|\d+r)")
BRANCHES = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "beqz", "bnez", "blez", "bgez", "bltz", "bgtz",
            "bgt", "ble", "bgtu", "bleu", "j"}
FP_ARITH = re.compile(r"^f(add|sub|mul|div|sqrt|min|max|n?m(add|sub)|sgnj|cvt|eq|lt|le|class|mv)\.")
LABEL = re.compile(r"^([A-Za-z_.$][\w.$]*):")


CLANG_CANDIDATES = ["/opt/homebrew/opt/llvm/bin/clang", "clang", "/usr/local/opt/llvm/bin/clang"]


def find_clang() -> str:
    """Return the first clang that can emit RISC-V (Apple clang cannot; Homebrew LLVM can)."""
    probe = "int f(int a){return a*3;}"
    for c in CLANG_CANDIDATES:
        try:
            r = subprocess.run([c, "--target=riscv64-linux-gnu", "-march=rv64gc", "-S", "-o", "-", "-x", "c", "-"],
                               input=probe, capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if r.returncode == 0:
            return c
    raise SystemExit("no clang with the RISC-V backend found")


CLANG = find_clang()


def compile_asm(src: str, march: str) -> Path:
    ASM_DIR.mkdir(parents=True, exist_ok=True)
    out = ASM_DIR / f"{Path(src).stem}.{march}.s"
    cmd = [CLANG, "--target=riscv64-linux-gnu", f"-march={march}", "-mabi=lp64d", "-O2", "-S",
           "-ffreestanding", "-fno-math-errno", "-fno-exceptions", "-fno-rtti", "-std=c++20", "-x", "c++",
           "-I", str(HERE), "-o", str(out), str(HERE / src)]
    subprocess.run(cmd, check=True)
    return out


def function_body(asm: str, fname: str) -> list[str]:
    lines = asm.splitlines()
    start = next(i for i, l in enumerate(lines) if l.split("#")[0].strip() == f"{fname}:")
    body = []
    for l in lines[start + 1:]:
        if l.startswith(".Lfunc_end"):
            break
        body.append(l)
    return body


def classify(mn: str) -> str:
    if mn in LOADS or mn in STORES or VMEM.match(mn):
        return "mem"
    if mn.startswith("v"):
        return "vector"
    return "scalar"


def loop_multipliers(instrs: list[tuple[str, str]], label_at: dict[str, int], trips: list[int],
                     fname: str) -> tuple[list[int], list[dict]]:
    """Build the basic-block CFG, find natural loops (back edges whose target reaches the
    source), and return a per-instruction multiplier from the supplied trip counts."""
    n = len(instrs)
    starts = sorted({0, *label_at.values()} | {i + 1 for i, (mn, _) in enumerate(instrs)
                                                if (mn in BRANCHES or mn in ("ret", "tail")) and i + 1 < n})
    starts = [x for x in starts if x < n]
    block_of = {}
    blocks = []
    for bi, st in enumerate(starts):
        en = starts[bi + 1] if bi + 1 < len(starts) else n
        blocks.append((st, en))
        for i in range(st, en):
            block_of[i] = bi
    succ: list[list[int]] = [[] for _ in blocks]
    for bi, (st, en) in enumerate(blocks):
        mn, ops = instrs[en - 1]
        if mn in BRANCHES:
            tgt = ops.split(",")[-1].strip()
            if tgt in label_at and label_at[tgt] < n:
                succ[bi].append(block_of[label_at[tgt]])
            if mn != "j" and bi + 1 < len(blocks):
                succ[bi].append(bi + 1)
        elif mn in ("ret", "tail"):
            pass
        elif bi + 1 < len(blocks):
            succ[bi].append(bi + 1)
    pred: list[list[int]] = [[] for _ in blocks]
    for u, ss in enumerate(succ):
        for v in ss:
            pred[v].append(u)

    def reaches(a: int, b: int) -> bool:
        seen, stack = {a}, [a]
        while stack:
            x = stack.pop()
            if x == b:
                return True
            for y in succ[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    back = [(u, h) for u, ss in enumerate(succ) for h in ss if h <= u and reaches(h, u)]
    headers: dict[int, list[int]] = {}
    for u, h in back:
        headers.setdefault(h, []).append(u)
    if len(headers) != len(trips):
        raise SystemExit(f"{fname}: found {len(headers)} loops, expected {len(trips)} trip counts")
    bmult = [1] * len(blocks)
    loops = []
    for (h, latches), t in zip(sorted(headers.items()), trips):
        body = {h}
        stack = list(latches)
        while stack:
            x = stack.pop()
            if x not in body:
                body.add(x)
                stack.extend(pred[x])
        for b in body:
            bmult[b] *= t
        loops.append({"trip_count": t, "body_instructions": sum(blocks[b][1] - blocks[b][0] for b in body)})
    return [bmult[block_of[i]] for i in range(n)], loops


def count_function(asm: str, fname: str, trips: list[int], all_targets: dict) -> dict:
    body = function_body(asm, fname)
    instrs: list[tuple[str, str]] = []   # (mnemonic, operands)
    label_at: dict[str, int] = {}
    for l in body:
        s = l.strip()
        if not s or s.startswith("#"):
            continue
        m = LABEL.match(s)
        if m:
            label_at[m.group(1)] = len(instrs)
            continue
        if s.startswith("."):
            continue
        parts = s.split(None, 1)
        instrs.append((parts[0], parts[1] if len(parts) > 1 else ""))
    mult, loops = loop_multipliers(instrs, label_at, trips, fname)
    res = {"scalar": 0, "vector": 0, "mem": 0}
    fp_ops = 0
    flops = 0
    ext_calls: dict[str, int] = {}
    for (mn, ops), w in zip(instrs, mult):
        res[classify(mn)] += w
        if FP_ARITH.match(mn):
            fp_ops += w
            flops += w * (2 if re.match(r"^fn?m(add|sub)", mn) else 1)
        if mn in ("call", "tail"):
            callee = ops.split()[0].split("@")[0]
            if callee in all_targets and callee != fname:
                sub = all_targets[callee]
                for k in res:
                    res[k] += w * sub["instr"][k]
                flops += w * sub["flops_est"]
            else:
                ext_calls[callee] = ext_calls.get(callee, 0) + w
    return {
        "instr": res,
        "instr_total": sum(res.values()),
        "fp_ops": fp_ops,
        "flops_est": flops,
        "static_instructions": len(instrs),
        "code_bytes_upper": 4 * len(instrs),
        "loops": loops,
        "external_calls": ext_calls,
        "exact": not any(mn in BRANCHES and mn != "j" for mn, _ in instrs),
    }


def main() -> dict:
    ver = subprocess.run([CLANG, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    out: dict = {"_meta": {"compiler": ver, "flags": "--target=riscv64-linux-gnu -O2 -S -ffreestanding -fno-math-errno -fno-exceptions -fno-rtti -std=c++20",
                           "method": "static count of compiled RV64 assembly, loops weighted by known trip counts; "
                                     "both sides of forward branches counted (upper bound when 'exact' is false)"}}
    asm_cache: dict[tuple[str, str], str] = {}
    # Callees first so callers can inline their counts.
    order = sorted(TARGETS, key=lambda f: 0 if f == "rnea_gravity_coriolis" else 1)
    for fname in order:
        src, march, trips = TARGETS[fname]
        key = (src, march)
        if key not in asm_cache:
            asm_cache[key] = compile_asm(src, march).read_text()
        rec = count_function(asm_cache[key], fname, trips, out)
        rec.update({"source": f"workloads/capture/control/{src}", "march": march,
                    "source_sha256": hashlib.sha256((HERE / src).read_bytes()).hexdigest()})
        out[fname] = rec
    (HERE / "rv64_counts.json").write_text(json.dumps(out, indent=1) + "\n")
    for f in TARGETS:
        r = out[f]
        print(f"{f:24s} {r['march']:8s} instr/call {r['instr_total']:7d} {r['instr']} exact={r['exact']} ext={r['external_calls']}")
    return out


if __name__ == "__main__":
    main()
