"""T10_record: background logging/recording at a configurable bitrate, modeled as
DMA + DRAM streams (best effort).

Every recorded byte crosses DRAM three times: written by its producer into a
record ring, read by the recorder DMA, and read again by the storage-controller
DMA. Ops carry "bytes_scale_param": "bitrate_bps" so a simulator can rescale
bytes linearly with params.bitrate_bps (scenario event set_bitrate). A separate
T10_record_4k graph is also written at the raw 4K30 bitrate for scenario S7.

Run: .venv/bin/python workloads/capture/record.py
"""
from __future__ import annotations

from common import CPU, DMA, make_graph, op, provenance, write_graph

SCRIPT = "workloads/capture/record.py"
PERIOD_US = 33333
DEFAULT_BPS = 200e6                       # compressed multi-stream mcap logging (assumption)
RAW_4K30_BPS = 3840 * 2160 * 3 * 8 * 30   # 4K rgb8 at 30 fps, uncompressed: 5.97 Gbit/s


def graph(task_id: str, bps: float, label: str) -> dict:
    B = bps / 8 * PERIOD_US * 1e-6
    ops = [
        op("rec.gather", "memcopy", DMA, dtype="int8", bytes_in=B, bytes_out=B, bytes_scale_param="bitrate_bps",
           notes="collect this period's records from producer buffers into the record ring (DRAM read + write)",
           depends_on=[]),
        op("rec.index_crc", "cpu_kernel", CPU, dtype="int32", bytes_in=B, bytes_out=B / 1000,
           instr={"scalar": int(B / 8 * 4), "vector": 0, "mem": int(B / 8)}, bytes_scale_param="bitrate_bps",
           notes="mcap chunk index + CRC32 (slice-by-8: about 4 scalar instr and 1 load per 8 bytes), estimate"),
        op("rec.flush", "memcopy", DMA, dtype="int8", bytes_in=B, bytes_out=B, bytes_scale_param="bitrate_bps",
           notes="record ring -> storage controller (DRAM read)", preempt_boundary=True),
    ]
    ops[1]["depends_on"] = ["rec.gather"]
    ops[2]["depends_on"] = ["rec.index_crc"]
    prov = provenance(source=f"recording/logging stream at {label}", script=SCRIPT, method="analytic_count",
                      notes=("Best-effort DMA/DRAM stream. Bytes per job = bitrate x period; DRAM traffic about 3x the "
                             "bitrate. Default bitrate is an assumption; scale with params.bitrate_bps."))
    return make_graph(task_id, f"Recording at {label}", prov, ops, release_override={"period_us": PERIOD_US},
                      params={"bitrate_bps": bps, "bytes_scale_param": "bitrate_bps", "dram_passes": 3,
                              "dram_bytes_per_s": 3 * bps / 8},
                      footprint={"weights_bytes": 0, "working_set_bytes": int(4 * B)})


def main() -> None:
    for g in (graph("T10_record", DEFAULT_BPS, "200 Mbit/s (default)"),
              graph("T10_record_4k", RAW_4K30_BPS, "raw 4K30 (5.97 Gbit/s)")):
        write_graph(g)
        print(f"{g['task_id']}: {g['params']['dram_bytes_per_s']/1e9:.2f} GB/s DRAM")


if __name__ == "__main__":
    main()
