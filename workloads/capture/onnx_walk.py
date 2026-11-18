"""Walk an ONNX graph and emit Glass Chip op records.

What it does, in order:
1. Runs onnx.shape_inference so every tensor has a static shape.
2. Drops pure metadata nodes (Shape, Constant, Reshape, Unsqueeze, ...) and
   threads dependencies through them.
3. Fuses activations into their producing Conv/MatMul/Gemm, the way an NPU
   compiler would (SiLU = Sigmoid+Mul, Relu, Clip, HardSwish, HardSigmoid,
   Gelu, bias Add).
4. Counts MACs/FLOPs and bytes per op for a chosen deployment dtype
   (default int8 activations and weights, int32 bias).
5. Groups ops into "layers" (NPU preemption granularity) with a caller-supplied
   function of the node name; the last op of each layer is a preempt boundary.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import onnx
from onnx import numpy_helper, shape_inference

from common import NPU_MAC, NPU_MOVE, NPU_VEC

METADATA_OPS = {"Shape", "Constant", "ConstantOfShape", "Range", "Size", "NonZero", "Identity",
                "Reshape", "Squeeze", "Unsqueeze", "Flatten", "Cast", "CastLike", "Dropout"}
ALIAS_OPS = {"Reshape", "Squeeze", "Unsqueeze", "Flatten", "Identity", "Cast", "CastLike", "Dropout"}
MOVE_OPS = {"Concat", "Split", "Slice", "Transpose", "Pad", "Gather", "Expand", "Tile",
            "GatherElements", "ScatterND", "SpaceToDepth", "DepthToSpace", "Where"}
UNARY_COST = {"Sigmoid": 4, "Tanh": 4, "Exp": 4, "Erf": 4, "Log": 4, "Sqrt": 1, "Relu": 1, "Clip": 1,
              "HardSwish": 3, "HardSigmoid": 2, "Gelu": 8, "Neg": 1, "Reciprocal": 1, "Abs": 1,
              "LeakyRelu": 2, "Softplus": 4, "Sin": 4, "Cos": 4, "Floor": 1, "Ceil": 1, "Round": 1,
              "Not": 1, "IsNaN": 1}
BINARY_OPS = {"Add", "Sub", "Mul", "Div", "Pow", "Max", "Min", "Equal", "Less", "Greater",
              "LessOrEqual", "GreaterOrEqual", "And", "Or", "Mod"}
FUSABLE_ACT = {"Relu", "Clip", "HardSwish", "HardSigmoid", "Gelu", "Sigmoid", "Tanh", "LeakyRelu"}
MAC_OPS = {"Conv", "ConvTranspose", "MatMul", "Gemm"}


@dataclass
class WalkConfig:
    """Deployment assumptions for byte counting."""
    act_bytes: int = 1          # int8 activations
    w_bytes: int = 1            # int8 weights
    bias_bytes: int = 4         # int32 bias
    dtype: str = "int8"
    prefix: str = "net"
    layer_fn: Callable[[str], str] = field(default=lambda n: n.strip("/").split("/")[0] or "net")
    # Optional override: node name -> eligible engines / dtype
    eligible_fn: Callable[[str, str], list[str] | None] | None = None
    dtype_fn: Callable[[str, str], str | None] | None = None


def _numel(shape: list[int] | None) -> int:
    if not shape:
        return 1 if shape == [] else 0
    n = 1
    for d in shape:
        n *= max(int(d), 0)
    return n


def _shapes(model: onnx.ModelProto) -> tuple[dict[str, list[int]], dict[str, int]]:
    shapes: dict[str, list[int]] = {}
    elem: dict[str, int] = {}
    vis = list(model.graph.value_info) + list(model.graph.input) + list(model.graph.output)
    for vi in vis:
        t = vi.type.tensor_type
        if t.HasField("shape"):
            dims = []
            ok = True
            for d in t.shape.dim:
                if d.HasField("dim_value"):
                    dims.append(int(d.dim_value))
                else:
                    ok = False
                    dims.append(1)
            if ok:
                shapes[vi.name] = dims
        elem[vi.name] = t.elem_type
    for init in model.graph.initializer:
        shapes[init.name] = list(init.dims)
        elem[init.name] = init.data_type
    for node in model.graph.node:
        if node.op_type == "Constant":
            for a in node.attribute:
                if a.name == "value":
                    shapes[node.output[0]] = list(a.t.dims)
                    elem[node.output[0]] = a.t.data_type
    return shapes, elem


def _ort_shapes(model_path: str, names: list[str], elem: dict[str, int]) -> dict[str, list[int]]:
    """Fill shapes that static inference missed by running the model once in onnxruntime
    with zero inputs and every missing tensor exposed as a graph output."""
    import onnxruntime as ort
    m = onnx.load(model_path)
    existing = {o.name for o in m.graph.output}
    for nm in names:
        if nm not in existing:
            m.graph.output.append(onnx.helper.make_empty_tensor_value_info(nm))
    sess = ort.InferenceSession(m.SerializeToString(), providers=["CPUExecutionProvider"])
    feeds = {}
    for i in sess.get_inputs():
        shp = [d if isinstance(d, int) else 1 for d in i.shape]
        dt = np.float32 if "float" in i.type else np.int64
        feeds[i.name] = np.zeros(shp, dtype=dt)
    outs = sess.run(names, feeds)
    res = {}
    for nm, arr in zip(names, outs):
        res[nm] = list(np.asarray(arr).shape)
        elem.setdefault(nm, onnx.helper.np_dtype_to_tensor_dtype(np.asarray(arr).dtype))
    return res


def _attr(node: onnx.NodeProto, name: str, default: Any = None) -> Any:
    for a in node.attribute:
        if a.name == name:
            return onnx.helper.get_attribute_value(a)
    return default


def _sanitize(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.]+", ".", name.strip("/")).strip(".")
    return re.sub(r"\.+", ".", s)


def walk(model_path: str, cfg: WalkConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (ops, stats) for an ONNX model file."""
    model = onnx.load(model_path)
    model = shape_inference.infer_shapes(model, data_prop=True)
    shapes, elem = _shapes(model)
    g = model.graph
    missing = [o for n in g.node for o in n.output if o and o not in shapes and n.op_type != "Constant"]
    missing += [i.name for i in g.input if i.name not in shapes]
    unresolved: list[str] = []
    if missing:
        try:
            shapes.update(_ort_shapes(model_path, missing, elem))
        except Exception:  # some exported graphs do not load in onnxruntime
            unresolved = missing
    weights = {i.name for i in g.initializer}
    const_outs = {n.output[0] for n in g.node if n.op_type == "Constant"}
    weights |= const_outs

    producers: dict[str, onnx.NodeProto] = {}
    consumers: dict[str, list[onnx.NodeProto]] = defaultdict(list)
    for n in g.node:
        for o in n.output:
            producers[o] = n
        for i in n.input:
            if i:
                consumers[i].append(n)

    INT_TYPES = {onnx.TensorProto.INT64, onnx.TensorProto.INT32, onnx.TensorProto.BOOL}

    def is_meta(n: onnx.NodeProto) -> bool:
        if n.op_type in METADATA_OPS and n.op_type not in ALIAS_OPS:
            return True
        # Shape arithmetic on small integer tensors (e.g. Gather/Concat over shapes).
        outs = [o for o in n.output if o]
        if outs and all(elem.get(o) in INT_TYPES and _numel(shapes.get(o)) <= 16 for o in outs):
            return True
        # Ops whose every non-empty input is a weight/constant fold at compile time.
        ins = [i for i in n.input if i]
        if ins and all(i in weights for i in ins) and n.op_type not in MAC_OPS:
            return True
        return False

    # Constant folding closure: outputs of meta nodes on constants become weights.
    changed = True
    meta_nodes: set[str] = set()
    while changed:
        changed = False
        for n in g.node:
            key = n.name or n.output[0]
            if key in meta_nodes:
                continue
            if is_meta(n):
                meta_nodes.add(key)
                ins = [i for i in n.input if i]
                if ins and all(i in weights for i in ins):
                    for o in n.output:
                        weights.add(o)
                changed = True

    # Fusion: activation (and bias Add) into MAC producers.
    fused_into: dict[str, str] = {}  # node key -> host node key

    def key(n: onnx.NodeProto) -> str:
        return n.name or n.output[0]

    for n in g.node:
        if n.op_type not in MAC_OPS:
            continue
        host = key(n)
        out = n.output[0]
        for _ in range(3):
            cons = consumers.get(out, [])
            if not cons:
                break
            # bias add: MatMul -> Add(initializer)
            if len(cons) == 1 and cons[0].op_type == "Add" and n.op_type == "MatMul":
                other = [i for i in cons[0].input if i != out]
                if other and other[0] in weights and len(shapes.get(other[0], [])) <= 1:
                    fused_into[key(cons[0])] = host
                    out = cons[0].output[0]
                    continue
            # SiLU: Sigmoid(x) and Mul(x, sigmoid(x))
            if len(cons) == 2:
                ops = {c.op_type: c for c in cons}
                if set(ops) == {"Sigmoid", "Mul"}:
                    s, m = ops["Sigmoid"], ops["Mul"]
                    if set(m.input) == {out, s.output[0]} and len(consumers.get(s.output[0], [])) == 1:
                        fused_into[key(s)] = host
                        fused_into[key(m)] = host
                        out = m.output[0]
                        continue
            if len(cons) == 1 and cons[0].op_type in FUSABLE_ACT:
                fused_into[key(cons[0])] = host
                out = cons[0].output[0]
                continue
            break

    # Map tensor -> set of kept-op keys that produced it (through aliases/meta).
    kept: list[onnx.NodeProto] = []
    src_of: dict[str, set[str]] = {}

    def tensor_src(t: str) -> set[str]:
        if t in weights or t not in producers:
            return set()
        return src_of.get(t, set())

    host_out: dict[str, list[str]] = defaultdict(list)
    for n in g.node:
        k = key(n)
        if k in meta_nodes:
            deps: set[str] = set()
            for i in n.input:
                if i:
                    deps |= tensor_src(i)
            for o in n.output:
                src_of[o] = deps if n.op_type in ALIAS_OPS else set()
            continue
        if n.op_type in ALIAS_OPS:
            deps = set()
            for i in n.input[:1]:
                deps |= tensor_src(i)
            for o in n.output:
                src_of[o] = deps
            continue
        target = fused_into.get(k, k)
        for o in n.output:
            src_of[o] = {target}
        if k not in fused_into:
            kept.append(n)
        host_out[target].append(n.output[0])

    by_key = {key(n): n for n in g.node}
    fused_members: dict[str, list[onnx.NodeProto]] = defaultdict(list)
    for k, h in fused_into.items():
        fused_members[h].append(by_key[k])

    ops: list[dict[str, Any]] = []
    id_of: dict[str, str] = {}
    counter = 0
    stats = {"onnx_nodes": len(g.node), "dropped_metadata": len(meta_nodes),
             "fused": len(fused_into), "ort_filled_shapes": len(missing) - len(unresolved),
             "unresolved_shapes": unresolved}

    for n in kept:
        k = key(n)
        members = [n] + fused_members.get(k, [])
        member_outs = {o for m in members for o in m.output}
        rec = _count(n, shapes, weights, cfg)
        for m in members[1:]:
            extra = _count(m, shapes, weights, cfg)
            rec["flops"] += extra["flops"]
            rec["bytes_w"] += extra["bytes_w"]
        # External activation inputs of the fused group.
        ext_in = []
        for m in members:
            for i in m.input:
                if i and i not in weights and i not in member_outs:
                    ext_in.append(i)
        ext_in = [i for i in ext_in if not (elem.get(i) in INT_TYPES and _numel(shapes.get(i)) <= 16)]
        rec["bytes_in"] = sum(_numel(shapes.get(i)) for i in dict.fromkeys(ext_in)) * cfg.act_bytes
        final_out = host_out[k][-1] if host_out[k] else n.output[0]
        outs = [final_out] + [o for o in n.output[1:] if o]
        rec["bytes_out"] = sum(_numel(shapes.get(o)) for o in outs) * cfg.act_bytes
        layer = cfg.layer_fn(n.name or n.output[0])
        counter += 1
        op_id = f"{cfg.prefix}.{_sanitize(layer)}.{counter:03d}.{n.op_type.lower()}"
        id_of[k] = op_id
        deps: set[str] = set()
        for m in members:
            for i in m.input:
                if i and i not in member_outs:
                    deps |= tensor_src(i)
        deps.discard(k)
        rec.update({"op_id": op_id, "layer": f"{cfg.prefix}.{_sanitize(layer)}",
                    "depends_on": sorted(id_of[d] for d in deps if d in id_of)})
        if len(members) > 1:
            rec["onnx_ops"] = [m.op_type for m in members]
        if cfg.eligible_fn:
            e = cfg.eligible_fn(n.name, n.op_type)
            if e:
                rec["eligible"] = e
        if cfg.dtype_fn:
            d = cfg.dtype_fn(n.name, n.op_type)
            if d:
                rec["dtype"] = d
        ops.append(rec)

    # Preempt boundaries at the last op of each layer (in list order).
    for i, o in enumerate(ops):
        nxt = ops[i + 1]["layer"] if i + 1 < len(ops) else None
        o["preempt_boundary"] = o["layer"] != nxt
    stats["kept_ops"] = len(ops)
    return ops, stats


def _count(n: onnx.NodeProto, shapes: dict[str, list[int]], weights: set[str], cfg: WalkConfig) -> dict[str, Any]:
    t = n.op_type
    out_shape = shapes.get(n.output[0], [])
    out_n = _numel(out_shape)
    w_bytes = 0
    for idx, i in enumerate(n.input):
        if i and i in weights:
            ne = _numel(shapes.get(i))
            if t in ("Conv", "ConvTranspose", "Gemm") and idx == 2:
                w_bytes += ne * cfg.bias_bytes
            elif ne > 1:
                w_bytes += ne * cfg.w_bytes
    rec: dict[str, Any] = {"type": "other", "dtype": cfg.dtype, "macs": 0, "flops": 0,
                           "bytes_w": w_bytes, "shape": {}, "eligible": NPU_VEC}
    if t == "Conv":
        w = shapes.get(n.input[1], [])
        groups = int(_attr(n, "group", 1))
        K, Cg, R, S = (w + [1, 1, 1, 1])[:4]
        N, _, Ho, Wo = (out_shape + [1, 1, 1, 1])[:4]
        in_shape = shapes.get(n.input[0], [1, 1, 1, 1])
        C = in_shape[1] if len(in_shape) > 1 else Cg * groups
        rec["macs"] = N * K * Ho * Wo * Cg * R * S
        rec["type"] = "dwconv2d" if groups > 1 and groups == C else "conv2d"
        strides = _attr(n, "strides", [1, 1])
        rec["shape"] = {"N": N, "C": C, "H": in_shape[2] if len(in_shape) > 2 else 1,
                        "W": in_shape[3] if len(in_shape) > 3 else 1, "K": K, "R": R, "S": S,
                        "stride": int(strides[0]), "groups": groups, "Ho": Ho, "Wo": Wo}
        rec["eligible"] = NPU_MAC
    elif t == "ConvTranspose":
        w = shapes.get(n.input[1], [])
        in_shape = shapes.get(n.input[0], [1, 1, 1, 1])
        groups = int(_attr(n, "group", 1))
        Cin, Kg, R, S = (w + [1, 1, 1, 1])[:4]
        rec["macs"] = _numel(in_shape) * Kg * R * S
        rec["type"] = "conv2d"
        rec["shape"] = {"N": in_shape[0], "C": Cin, "K": Kg * groups, "R": R, "S": S, "transposed": True}
        rec["eligible"] = NPU_MAC
    elif t in ("MatMul", "Gemm"):
        a = shapes.get(n.input[0], [])
        b = shapes.get(n.input[1], [])
        if t == "Gemm":
            M, K = (a[1], a[0]) if _attr(n, "transA", 0) else (a[0], a[1])
            Nn = b[0] if _attr(n, "transB", 0) else b[1]
            batch = 1
        else:
            M = a[-2] if len(a) >= 2 else 1
            K = a[-1]
            Nn = b[-1] if len(b) >= 2 else 1
            batch = _numel(out_shape[:-2]) if len(out_shape) > 2 else 1
        rec["macs"] = batch * M * K * Nn
        rec["type"] = "matmul"
        act_x_act = n.input[1] not in weights
        rec["shape"] = {"M": M, "K": K, "N": Nn, "batch": batch}
        if act_x_act:
            rec["shape"]["act_x_act"] = True
        rec["eligible"] = NPU_MAC
    elif t in UNARY_COST:
        rec["flops"] = out_n * UNARY_COST[t]
        rec["type"] = "elementwise"
    elif t in BINARY_OPS:
        rec["flops"] = out_n
        rec["type"] = "elementwise"
    elif t == "Softmax" or t == "LogSoftmax":
        rec["flops"] = out_n * 5
        rec["type"] = "softmax"
    elif t in ("LayerNormalization", "BatchNormalization", "InstanceNormalization",
               "GroupNormalization", "SimplifiedLayerNormalization", "RMSNormalization"):
        rec["flops"] = out_n * 8
        rec["type"] = "norm"
    elif t in ("MaxPool", "AveragePool"):
        k = _attr(n, "kernel_shape", [1, 1])
        rec["flops"] = out_n * int(np.prod(k))
        rec["type"] = "pool"
        rec["shape"] = {"kernel": list(k)}
    elif t in ("GlobalAveragePool", "GlobalMaxPool"):
        rec["flops"] = _numel(shapes.get(n.input[0]))
        rec["type"] = "pool"
    elif t.startswith("Reduce") or t in ("ArgMax", "ArgMin", "TopK", "CumSum"):
        rec["flops"] = _numel(shapes.get(n.input[0]))
        rec["type"] = "reduce"
    elif t == "Resize" or t == "Upsample":
        mode = _attr(n, "mode", b"nearest")
        mode = mode.decode() if isinstance(mode, bytes) else str(mode)
        rec["flops"] = out_n * (4 if "linear" in mode else 1)
        rec["type"] = "resize"
        rec["shape"] = {"mode": mode}
    elif t in MOVE_OPS:
        rec["type"] = "memcopy"
        rec["eligible"] = NPU_MOVE
    else:
        rec["flops"] = out_n
        rec["type"] = "other"
    if out_shape:
        rec["shape"]["out"] = out_shape
    rec["shape"]["onnx_op"] = t
    return rec
