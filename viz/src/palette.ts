// Shared task color palette. This file is the single source of truth: the Python
// dev-replay generator parses the TASK_COLORS block below, so keep one entry per line.
// Colors are tuned for a dark background and stay distinguishable at small sizes.

export const TASK_COLORS: Record<string, string> = {
  T1_ingest_wrist: '#5EC8F2',
  T1_ingest_overhead: '#3B8EEA',
  T2_detect: '#7D8BFF',
  T2_detect_overhead: '#9AA6FF',
  T3_esdf: '#2ED3B0',
  T4_vla: '#B47CFF',
  T5_plan: '#FF8BB4',
  T6_estimate: '#FFD166',
  T7_control: '#FF9F45',
  T8_safety: '#A8E35C',
  T10_record: '#A9BCD0',
  T10_record_4k: '#C7D3E2',
  T11_speech: '#6FE3E3',
  T12_arm2_control: '#FFB48C',
  T13_map_save: '#CDAEFF',
  T14_ota: '#86DBA6',
  T15_diag: '#DCE3EC',
};

export const MISS_RED = '#FF4D5E';
export const OK_GREEN = '#3DDC97';

/** Color for a task id, falling back to the manifest color or a neutral tone. */
export function taskColor(id: string, fallback?: string): string {
  return TASK_COLORS[id] ?? fallback ?? '#C8D2DE';
}

/** Voltage tint: cool blue at low V, warm amber near nominal, red when throttled. */
export function voltageTint(v: number, vmin: number, vnom: number, throttled: boolean): [number, number, number] {
  if (throttled) return [1.0, 0.25, 0.3];
  const t = Math.max(0, Math.min(1, (v - vmin) / Math.max(1e-6, vnom - vmin)));
  const cool: [number, number, number] = [0.2, 0.52, 1.0];
  const mid: [number, number, number] = [0.55, 0.62, 0.85];
  const warm: [number, number, number] = [1.0, 0.68, 0.28];
  const a = t < 0.5 ? cool : mid;
  const b = t < 0.5 ? mid : warm;
  const k = t < 0.5 ? t * 2 : (t - 0.5) * 2;
  return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
}

/** Pipeline order used for rings and timeline rows. */
export const TASK_ORDER = ['T1_ingest_wrist', 'T1_ingest_overhead', 'T2_detect', 'T2_detect_overhead', 'T3_esdf', 'T4_vla',
  'T5_plan', 'T6_estimate', 'T7_control', 'T8_safety', 'T10_record', 'T10_record_4k', 'T11_speech', 'T12_arm2_control',
  'T13_map_save', 'T14_ota', 'T15_diag'];

export function taskRank(id: string): number {
  const i = TASK_ORDER.indexOf(id);
  return i < 0 ? 100 : i;
}
