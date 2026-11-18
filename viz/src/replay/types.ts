// Replay manifest and decoded data types. See viz/REPLAY_FORMAT.md.

export interface Floorplan { x: number; y: number; w: number; h: number }

export interface BlockInfo {
  id: string;
  kind: string;
  nodes?: number[][];
  power_domain?: string;
  floorplan: Floorplan;
  [k: string]: unknown;
}

export interface DomainInfo { v_min: number; v_max: number; opp_volts: number[] }

export interface TaskInfo {
  id: string;
  name: string;
  period_us: number | null;
  deadline_us: number | null;
  criticality: string;
  color?: string;
}

export interface ChunkInfo { t0_ps: number; t1_ps: number; max_end_ps: number; file: string; count: number }

export interface SeriesInfo {
  name: string; label: string; unit: string; group: string;
  t0_ps: number; rate_hz: number; offset: number; count: number;
}

export interface UnitInfo { block: string; block_index: number; unit: number }

export interface TaskResult {
  jobs: number; misses: number; miss_rate: number;
  p50_ms: number | null; p95_ms: number | null; p99_ms: number | null; max_ms: number | null;
}

export interface Results {
  source?: string;
  tasks?: Record<string, TaskResult>;
  avg_power_w?: number;
  peak_power_w?: number;
  energy_j?: number;
  energy_per_robot_hour_wh?: number | null;
  peak_temp_c?: number;
  [k: string]: unknown;
}

export interface RobotEvent { t_ps: number; kind: string; object?: string; latency_ms?: number; [k: string]: unknown }

export interface Manifest {
  format: string;
  version: number;
  name: string;
  scenario: string;
  scenario_label: string;
  policy: string;
  policy_label: string;
  seed: number;
  synthetic: boolean;
  duration_ps: number;
  created: string;
  provenance: Record<string, unknown>;
  soc: {
    name: string;
    process: { v_nominal?: number; [k: string]: unknown };
    noc: { dims: number[]; [k: string]: unknown };
    blocks: BlockInfo[];
    power_domains: Record<string, DomainInfo>;
  };
  tasks: TaskInfo[];
  events: { record_bytes: number; kinds: string[]; chunk_ps: number; chunks: ChunkInfo[]; strings: string[]; count: number };
  jobs: { file: string; record_bytes: number; count: number; attribution_keys: string[]; critpath_file: string };
  series: { file: string; items: SeriesInfo[] };
  lod: { file: string; bin_ps: number; bins: number; units: UnitInfo[]; links: number[][];
    busy_offset: number; busy_bytes: number; link_offset: number };
  robot: null | { fps: number; frames: number; bodies: string[]; chunk_frames: number; chunks: string[];
    events: RobotEvent[]; scene: string | null };
  results: Results;
  scenario_events?: { t_s: number; action: string; label: string }[];
  latency_tolerance?: { synthetic?: boolean; conveyor_speed_mps?: number; note?: string; latency_ms: number[]; success: number[] };
  surrogate?: { model?: string; [k: string]: unknown };
}

export interface ReplayIndexEntry {
  id: string; path: string; scenario: string; scenario_label: string;
  policy: string; policy_label: string; seed: number; synthetic: boolean; default?: boolean;
}

export interface ReplayIndex {
  format: string; version: number; replays: ReplayIndexEntry[];
  surrogate?: { model?: string; meta?: string };
}

/** Struct-of-arrays chip events. Times are picoseconds stored as float64 (exact below 2^53). */
export interface EventBlock {
  n: number;
  t: Float64Array;
  dur: Float64Array;
  kind: Uint8Array;
  block: Uint8Array;
  unit: Uint16Array;
  task: Uint16Array;
  src: Uint16Array;
  dst: Uint16Array;
  flags: Uint16Array;
  job: Uint32Array;
  op: Uint32Array;
  bytes: Float32Array;
  value: Float32Array;
  aux: Uint32Array;
}

export interface JobTable {
  n: number;
  job: Uint32Array;
  task: Uint16Array;
  flags: Uint16Array;
  release: Float64Array;
  start: Float64Array;
  finish: Float64Array;
  deadline: Float64Array;
  attrNs: Float32Array; // n * nAttr
  nAttr: number;
}

export interface CritOp { op_id: string; engine: string; start_ps: number; end_ps: number; wait_ps: number; wait_cause: string }

export const KIND = { job_release: 0, job_start: 1, job_finish: 2, job_miss: 3, op: 4, xfer: 5, dram_q: 6, freq: 7, preempt: 8 } as const;
export const FLAG_CRITICAL = 1;
export const FLAG_MISSED = 2;
export const JOB_MISSED = 1;
export const JOB_HAS_CRITPATH = 4;
export const NONE16 = 0xffff;
