"""Run SCALE-Sim on one layer (subprocess worker for calib/run_scalesim.py).

    python run_layer.py <cfg> <workdir> <name> <ifmap_h> <ifmap_w> <R> <S> <C> <K> <stride>

Prints one JSON object with SCALE-Sim's compute, bandwidth and detailed access report
fields for the layer. Traces are not saved (save_disk_space=True).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
from pathlib import Path

# Use the patched clone in third_party (numpy 2 fixes), not the site-packages copy.
SCALESIM_SRC = Path(__file__).resolve().parents[2] / "third_party" / "scale-sim-v2"
sys.path.insert(0, str(SCALESIM_SRC))


def main() -> None:
    cfg, workdir, name, h, w, r, s, c, k, stride = sys.argv[1:11]
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    topo = wd / "topo.csv"
    topo.write_text("Layer name, IFMAP Height, IFMAP Width, Filter Height, Filter Width, "
                    "Channels, Num Filter, Strides,\n"
                    f"{name},{h},{w},{r},{s},{c},{k},{stride},\n")
    # SCALE-Sim always loads a layout file; with IfmapCustomLayout/FilterCustomLayout False
    # it is ignored, so I write the stock example row under this layer's name.
    layout = wd / "layout.csv"
    layout.write_text(
        "Layer name, IFMAP Height Intraline Factor, IFMAP Width Intraline Factor, Filter Height "
        "Intraline Factor, Filter Width Intraline Factor, Channel Intraline Factor, Num Filter "
        "Intraline Factor, IFMAP Height Intraline Order, IFMAP Width Intraline Order, Channel "
        "Intraline Order, IFMAP Height Interline Order, IFMAP Width Interline Order, Channel "
        "Interline Order, Num Filter Intraline Order, Channel Intraline Order, Filter Height "
        "Intraline Order, Filter Width Intraline Order, Num Filter Interline Order, Channel "
        "Interline Order, Filter Height Interline Order, Filter Width Interline Order,\n"
        f"{name}, 2, 2, 1, 1, 16, 4, 0, 1, 2, 4, 5, 3, 3, 2, 1, 0, 4, 5, 6, 7,\n")
    from scalesim.scale_sim import scalesim

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sim = scalesim(save_disk_space=True, verbose=False, config=cfg, topology=str(topo),
                       layout=str(layout), input_type_gemm=False)
        sim.run_scale(top_path=str(wd))
    run_dirs = [p for p in wd.iterdir() if p.is_dir()]
    rd = run_dirs[0]

    def read(fn: str) -> dict[str, str]:
        with open(rd / fn) as f:
            rows = list(csv.reader(f))
        hdr = [x.strip() for x in rows[0] if x.strip()]
        vals = [x.strip() for x in rows[1] if x.strip()]
        return dict(zip(hdr, vals))

    out = {"compute": read("COMPUTE_REPORT.csv"), "bandwidth": read("BANDWIDTH_REPORT.csv"),
           "detail": read("DETAILED_ACCESS_REPORT.csv")}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
