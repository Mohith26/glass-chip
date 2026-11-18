// ReplayStore: manifest + lazily loaded, time-indexed chunks.
import type { CritOp, EventBlock, JobTable, Manifest, ReplayIndexEntry, SeriesInfo } from './types';
import { JOB_MISSED } from './types';

type Pending = { resolve: (v: unknown) => void; reject: (e: unknown) => void };

class WorkerClient {
  private w: Worker;
  private seq = 0;
  private pending = new Map<number, Pending>();
  constructor() {
    this.w = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' });
    this.w.onmessage = (ev: MessageEvent<{ id: number; ok: boolean; data?: unknown; error?: string }>) => {
      const p = this.pending.get(ev.data.id);
      if (!p) return;
      this.pending.delete(ev.data.id);
      if (ev.data.ok) p.resolve(ev.data.data);
      else p.reject(new Error(ev.data.error));
    };
  }
  call<T>(msg: Record<string, unknown>): Promise<T> {
    const id = ++this.seq;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.w.postMessage({ ...msg, id });
    });
  }
}

let sharedWorker: WorkerClient | null = null;
function worker(): WorkerClient {
  if (!sharedWorker) sharedWorker = new WorkerClient();
  return sharedWorker;
}

export interface JobView {
  index: number; job: number; task: number; release: number; start: number; finish: number; deadline: number;
  missed: boolean; hasCrit: boolean;
}

export class ReplayStore {
  readonly base: string;
  manifest!: Manifest;
  jobs!: JobTable;
  series = new Map<string, Float32Array>();
  seriesInfo = new Map<string, SeriesInfo>();
  lodBusy!: Uint8Array;
  lodLinks!: Float32Array;
  readonly eventChunks = new Map<number, EventBlock>();
  /** Bumped whenever a chunk finishes loading. */
  version = 0;
  private inflight = new Set<Promise<unknown>>();

  /** Track a background load so capture mode can wait for it. */
  protected track<T>(p: Promise<T>): Promise<T> {
    this.inflight.add(p);
    void p.finally(() => this.inflight.delete(p));
    return p;
  }

  /** Resolves when no chunk or robot load is in flight. */
  async idle(): Promise<void> {
    while (this.inflight.size) await Promise.allSettled([...this.inflight]);
  }
  private eventLoading = new Map<number, Promise<EventBlock>>();
  readonly robotChunks = new Map<number, Float32Array>();
  private robotLoading = new Set<number>();
  private critpaths: Record<string, CritOp[]> | null = null;
  /** Job indices per task, sorted by release. */
  jobsByTask: Int32Array[] = [];
  missIndices: number[] = [];
  readonly entry: ReplayIndexEntry | null;

  constructor(manifestUrl: string, entry: ReplayIndexEntry | null) {
    this.base = new URL('.', new URL(manifestUrl, location.href)).href;
    this.entry = entry;
  }

  url(rel: string): string {
    return new URL(rel, this.base).href;
  }

  async load(manifestUrl: string, onProgress?: (msg: string) => void): Promise<void> {
    const r = await fetch(manifestUrl);
    if (!r.ok) throw new Error(`manifest ${r.status}`);
    this.manifest = (await r.json()) as Manifest;
    const m = this.manifest;
    onProgress?.('Loading jobs');
    const w = worker();
    const [jobs, series, lod] = await Promise.all([
      w.call<JobTable>({ type: 'jobs', url: this.url(m.jobs.file), nAttr: m.jobs.attribution_keys.length }),
      w.call<ArrayBuffer>({ type: 'raw', url: this.url(m.series.file), gz: true }),
      w.call<ArrayBuffer>({ type: 'raw', url: this.url(m.lod.file), gz: true }),
    ]);
    this.jobs = jobs;
    for (const s of m.series.items) {
      this.series.set(s.name, new Float32Array(series, s.offset, s.count));
      this.seriesInfo.set(s.name, s);
    }
    this.lodBusy = new Uint8Array(lod, m.lod.busy_offset, m.lod.busy_bytes);
    const nl = m.lod.links.length * m.lod.bins;
    this.lodLinks = new Float32Array(lod, m.lod.link_offset, nl);
    const per: number[][] = m.tasks.map(() => []);
    for (let i = 0; i < jobs.n; i++) {
      per[jobs.task[i]]?.push(i);
      if (jobs.flags[i] & JOB_MISSED) this.missIndices.push(i);
    }
    this.jobsByTask = per.map((a) => Int32Array.from(a));
    onProgress?.('Loading first chunks');
    await Promise.all([this.ensureEvents(0, Math.min(m.duration_ps, m.events.chunk_ps)), this.ensureRobot(0)]);
  }

  get duration(): number {
    return this.manifest.duration_ps;
  }

  jobView(i: number): JobView {
    const j = this.jobs;
    return {
      index: i, job: j.job[i], task: j.task[i], release: j.release[i], start: j.start[i], finish: j.finish[i],
      deadline: j.deadline[i], missed: (j.flags[i] & JOB_MISSED) !== 0, hasCrit: (j.flags[i] & 4) !== 0,
    };
  }

  /** Latest job of a task released at or before t (index into jobs table), or -1. */
  latestJob(task: number, t: number): number {
    const arr = this.jobsByTask[task];
    if (!arr || arr.length === 0) return -1;
    let lo = 0, hi = arr.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.jobs.release[arr[mid]] <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best < 0 ? -1 : arr[best];
  }

  /** Position (in jobsByTask[task]) of the first job released at or after t. */
  firstJobPos(task: number, t: number): number {
    const arr = this.jobsByTask[task];
    let lo = 0, hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.jobs.release[arr[mid]] < t) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  findJobById(jobId: number): number {
    // job ids are mostly increasing with release; linear fallback is fine for UI actions
    for (let i = 0; i < this.jobs.n; i++) if (this.jobs.job[i] === jobId) return i;
    return -1;
  }

  attribution(i: number): Record<string, number> {
    const keys = this.manifest.jobs.attribution_keys;
    const out: Record<string, number> = {};
    keys.forEach((k, a) => { out[k] = this.jobs.attrNs[i * keys.length + a]; });
    return out;
  }

  async critPath(jobId: number): Promise<CritOp[] | null> {
    if (!this.critpaths) {
      this.critpaths = await worker().call<Record<string, CritOp[]>>({ type: 'json', url: this.url(this.manifest.jobs.critpath_file), gz: true });
    }
    return this.critpaths[String(jobId)] ?? null;
  }

  /** Sample a uniformly sampled series at time t (linear interpolation). NaN if missing. */
  sample(name: string, t: number): number {
    const s = this.series.get(name);
    const info = this.seriesInfo.get(name);
    if (!s || !info || s.length === 0) return NaN;
    const x = ((t - info.t0_ps) / 1e12) * info.rate_hz;
    const i = Math.max(0, Math.min(s.length - 1, Math.floor(x)));
    const j = Math.min(s.length - 1, i + 1);
    const f = Math.max(0, Math.min(1, x - i));
    return s[i] * (1 - f) + s[j] * f;
  }

  seriesNames(prefix: string): string[] {
    return [...this.series.keys()].filter((k) => k.startsWith(prefix));
  }

  // ---------------------------------------------------------------- events
  chunkRange(t0: number, t1: number): number[] {
    const out: number[] = [];
    this.manifest.events.chunks.forEach((c, i) => {
      if (c.t0_ps <= t1 && c.max_end_ps >= t0) out.push(i);
    });
    return out;
  }

  ensureEvents(t0: number, t1: number): Promise<EventBlock[]> {
    return Promise.all(this.chunkRange(t0, t1).map((i) => this.loadChunk(i)));
  }

  private loadChunk(i: number): Promise<EventBlock> {
    const have = this.eventChunks.get(i);
    if (have) return Promise.resolve(have);
    let p = this.eventLoading.get(i);
    if (!p) {
      p = worker().call<EventBlock>({ type: 'events', url: this.url(this.manifest.events.chunks[i].file) }).then((b) => {
        this.eventChunks.set(i, b);
        this.eventLoading.delete(i);
        this.version++;
        this.evictEvents(i);
        return b;
      });
      this.eventLoading.set(i, p);
      this.track(p);
    }
    return p;
  }

  private evictEvents(keep: number): void {
    if (this.eventChunks.size <= 24) return;
    for (const k of [...this.eventChunks.keys()]) {
      // keep a window around the newest chunk, plus the first two (the loop restarts there)
      if (Math.abs(k - keep) > 10 && k > 1) this.eventChunks.delete(k);
    }
  }

  /** Loaded event blocks overlapping [t0, t1] (only chunks already in memory). */
  loadedBlocks(t0: number, t1: number): EventBlock[] {
    const out: EventBlock[] = [];
    for (const i of this.chunkRange(t0, t1)) {
      const b = this.eventChunks.get(i);
      if (b) out.push(b);
    }
    return out;
  }

  /** True if every chunk overlapping the window is loaded. */
  windowLoaded(t0: number, t1: number): boolean {
    return this.chunkRange(t0, t1).every((i) => this.eventChunks.has(i));
  }

  // ---------------------------------------------------------------- robot
  ensureRobot(frame: number): Promise<void> {
    const r = this.manifest.robot;
    if (!r) return Promise.resolve();
    const c = Math.floor(frame / r.chunk_frames);
    const todo = [c, c + 1].filter((k) => k >= 0 && k < r.chunks.length && !this.robotChunks.has(k) && !this.robotLoading.has(k));
    return this.track(Promise.all(todo.map(async (k) => {
      this.robotLoading.add(k);
      const b = await worker().call<ArrayBuffer>({ type: 'raw', url: this.url(r.chunks[k]), gz: true });
      this.robotChunks.set(k, new Float32Array(b));
      this.robotLoading.delete(k);
    }))).then(() => undefined);
  }

  /** Body pose array (bodies*7) for a frame if loaded. */
  robotFrame(frame: number): Float32Array | null {
    const r = this.manifest.robot;
    if (!r) return null;
    const f = Math.max(0, Math.min(r.frames - 1, frame));
    const c = Math.floor(f / r.chunk_frames);
    const arr = this.robotChunks.get(c);
    if (!arr) return null;
    const stride = r.bodies.length * 7;
    const off = (f - c * r.chunk_frames) * stride;
    return arr.subarray(off, off + stride);
  }
}

export async function fetchJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return (await r.json()) as T;
}

export { worker as replayWorker };
