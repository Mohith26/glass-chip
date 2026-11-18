// LiveStore: a ReplayStore that grows as batches arrive from tools/live_server.py.
// Batches carry the same 48-byte event and 80-byte job records as replay files, so every view
// reads live and recorded data through the same fields.
import { concatEvents, decodeEvents, decodeJobs, indexEvents } from '../replay/decode';
import { ReplayStore } from '../replay/store';
import type { CritOp, EventBlock, Manifest, RobotEvent } from '../replay/types';
import { JOB_MISSED, KIND, NONE16 } from '../replay/types';

export interface LiveBatchMsg {
  type: 'batch';
  t_ps: number;
  events: Uint8Array;
  jobs: Uint8Array;
  critpaths: Record<string, CritOp[]>;
  strings_add: string[];
  series: Record<string, { t0_ps: number; rate_hz: number; unit: string; group: string; values: Uint8Array }>;
  robot: Uint8Array;
  robot_events: RobotEvent[];
  stats: { sim_s: number; wall_s: number; rate: number; raw_xfers: number; stored_xfers: number; policy: string; paused: boolean; fidelity: string };
}

function buf(u: Uint8Array): ArrayBuffer {
  return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
}

class Grow<T extends Float32Array | Float64Array | Uint8Array | Uint16Array | Uint32Array> {
  a: T;
  n = 0;
  constructor(private ctor: new (n: number) => T, cap = 1024) {
    this.a = new ctor(cap);
  }
  reserve(n: number): void {
    if (n <= this.a.length) return;
    let cap = this.a.length;
    while (cap < n) cap *= 2;
    const b = new this.ctor(cap);
    (b as unknown as { set(x: ArrayLike<number>): void }).set(this.a);
    this.a = b;
  }
  view(): T {
    return this.a.subarray(0, this.n) as T;
  }
}

export class LiveStore extends ReplayStore {
  stats: LiveBatchMsg['stats'] | null = null;
  private liveCrit = new Map<number, CritOp[]>();
  private seriesBufs = new Map<string, Grow<Float32Array>>();
  private busyAcc = new Grow(Float32Array, 1 << 16);
  private linkAcc = new Grow(Float32Array, 1 << 16);
  private busyU8 = new Grow(Uint8Array, 1 << 16);
  private perTask: number[][] = [];
  private jobCols = {
    job: new Grow(Uint32Array), task: new Grow(Uint16Array), flags: new Grow(Uint16Array),
    release: new Grow(Float64Array), start: new Grow(Float64Array), finish: new Grow(Float64Array),
    deadline: new Grow(Float64Array), attr: new Grow(Float32Array, 10240),
  };
  private routes = new Map<number, number[]>();
  private frameCount = 0;

  constructor(manifest: Manifest) {
    super(location.href, null);
    this.manifest = manifest;
    const nAttr = manifest.jobs.attribution_keys.length;
    this.jobs = { n: 0, nAttr, job: new Uint32Array(0), task: new Uint16Array(0), flags: new Uint16Array(0),
      release: new Float64Array(0), start: new Float64Array(0), finish: new Float64Array(0), deadline: new Float64Array(0),
      attrNs: new Float32Array(0) };
    this.perTask = manifest.tasks.map(() => []);
    this.jobsByTask = manifest.tasks.map(() => new Int32Array(0));
    this.lodBusy = new Uint8Array(0);
    this.lodLinks = new Float32Array(0);
    const links = manifest.lod.links;
    const idx = new Map<string, number>();
    links.forEach(([a, b], i) => idx.set(`${a}-${b}`, i));
    const nx = manifest.soc.noc.dims[0];
    // XY routes for every node pair, as link indices (same rule as tools/replay_writer.xy_route)
    const nn = manifest.soc.noc.dims[0] * manifest.soc.noc.dims[1];
    for (let s = 0; s < nn; s++) {
      for (let d = 0; d < nn; d++) {
        if (s === d) continue;
        const hops: number[] = [];
        let x = s % nx, y = Math.floor(s / nx);
        const dx = d % nx, dy = Math.floor(d / nx);
        while (x !== dx) { const n = x + (dx > x ? 1 : -1); hops.push(idx.get(`${y * nx + x}-${y * nx + n}`) ?? -1); x = n; }
        while (y !== dy) { const n = y + (dy > y ? 1 : -1); hops.push(idx.get(`${y * nx + x}-${n * nx + x}`) ?? -1); y = n; }
        this.routes.set(s * 64 + d, hops.filter((h) => h >= 0));
      }
    }
  }

  override async load(): Promise<void> { /* data arrives through ingest() */ }
  override ensureEvents(t0: number, t1: number): Promise<EventBlock[]> {
    return Promise.resolve(this.loadedBlocks(t0, t1));
  }
  override ensureRobot(): Promise<void> { return Promise.resolve(); }
  override async critPath(jobId: number): Promise<CritOp[] | null> {
    return this.liveCrit.get(jobId) ?? null;
  }

  /** Apply one batch from the live server. */
  ingest(b: LiveBatchMsg): void {
    const m = this.manifest;
    this.stats = b.stats;
    m.events.strings.push(...(b.strings_add ?? []));
    m.duration_ps = Math.max(m.duration_ps, b.t_ps);
    for (const [k, v] of Object.entries(b.critpaths ?? {})) this.liveCrit.set(Number(k), v);
    this.ingestEvents(decodeEvents(buf(b.events)));
    this.ingestJobs(buf(b.jobs));
    this.ingestSeries(b.series ?? {});
    this.ingestRobot(buf(b.robot), b.robot_events ?? []);
    this.version++;
  }

  private ingestEvents(ev: EventBlock): void {
    if (ev.n === 0) return;
    const m = this.manifest;
    const cps = m.events.chunk_ps;
    // split by chunk of start time, append, re-index the touched chunks
    const groups = new Map<number, number[]>();
    for (let i = 0; i < ev.n; i++) {
      const c = Math.floor(ev.t[i] / cps);
      let g = groups.get(c);
      if (!g) { g = []; groups.set(c, g); }
      g.push(i);
    }
    for (const [c, idx] of groups) {
      const part = pick(ev, idx);
      const prev = this.eventChunks.get(c);
      const merged = prev ? concatEvents(prev, part) : part;
      this.eventChunks.set(c, indexEvents(merged));
      while (m.events.chunks.length <= c) {
        const k = m.events.chunks.length;
        m.events.chunks.push({ t0_ps: k * cps, t1_ps: (k + 1) * cps, max_end_ps: (k + 1) * cps, file: '', count: 0 });
      }
      let end = m.events.chunks[c].max_end_ps;
      for (let i = 0; i < part.n; i++) end = Math.max(end, part.t[i] + part.dur[i]);
      m.events.chunks[c].max_end_ps = end;
      m.events.chunks[c].count = merged.n;
    }
    m.events.count += ev.n;
    this.accumulateLod(ev);
  }

  private accumulateLod(ev: EventBlock): void {
    const m = this.manifest;
    const binPs = m.lod.bin_ps;
    const nu = m.lod.units.length;
    const nl = m.lod.links.length;
    const unitCol = new Map<string, number>();
    m.lod.units.forEach((u, i) => unitCol.set(`${u.block_index}:${u.unit}`, i));
    let maxBin = m.lod.bins;
    for (let i = 0; i < ev.n; i++) maxBin = Math.max(maxBin, Math.floor((ev.t[i] + ev.dur[i]) / binPs) + 1);
    const grow = (g: Grow<Float32Array> | Grow<Uint8Array>, width: number): void => {
      g.reserve(maxBin * width);
      g.n = maxBin * width;
    };
    grow(this.busyAcc, nu); grow(this.busyU8, nu); grow(this.linkAcc, nl);
    const busy = this.busyAcc.a, links = this.linkAcc.a;
    const touched = new Set<number>();
    for (let i = 0; i < ev.n; i++) {
      const k = ev.kind[i];
      const t0 = ev.t[i], t1 = t0 + Math.max(ev.dur[i], 1);
      const b0 = Math.floor(t0 / binPs), b1 = Math.floor((t1 - 1) / binPs);
      if (k === KIND.op) {
        const col = unitCol.get(`${ev.block[i]}:${ev.unit[i]}`);
        if (col === undefined) continue;
        for (let b = b0; b <= b1; b++) {
          const lo = Math.max(t0, b * binPs), hi = Math.min(t1, (b + 1) * binPs);
          if (hi > lo) { busy[b * nu + col] += (hi - lo) / binPs; touched.add(b); }
        }
      } else if (k === KIND.xfer && ev.src[i] !== NONE16 && ev.dst[i] !== NONE16) {
        const r = this.routes.get(ev.src[i] * 64 + ev.dst[i]);
        if (!r) continue;
        const per = ev.bytes[i] / (b1 - b0 + 1);
        for (let b = b0; b <= b1; b++) for (const l of r) links[b * nl + l] += per;
      }
    }
    const u8 = this.busyU8.a;
    for (const b of touched) for (let c = 0; c < nu; c++) u8[b * nu + c] = Math.min(255, Math.round(busy[b * nu + c] * 255));
    m.lod.bins = maxBin;
    this.lodBusy = this.busyU8.view();
    this.lodLinks = this.linkAcc.view();
  }

  private ingestJobs(b: ArrayBuffer): void {
    if (b.byteLength === 0) return;
    const nAttr = this.manifest.jobs.attribution_keys.length;
    const jt = decodeJobs(b, nAttr);
    const c = this.jobCols;
    const n0 = this.jobs.n, n1 = n0 + jt.n;
    for (const g of [c.job, c.task, c.flags, c.release, c.start, c.finish, c.deadline]) { g.reserve(n1); g.n = n1; }
    c.attr.reserve(n1 * nAttr); c.attr.n = n1 * nAttr;
    c.job.a.set(jt.job, n0); c.task.a.set(jt.task, n0); c.flags.a.set(jt.flags, n0);
    c.release.a.set(jt.release, n0); c.start.a.set(jt.start, n0); c.finish.a.set(jt.finish, n0);
    c.deadline.a.set(jt.deadline, n0); c.attr.a.set(jt.attrNs, n0 * nAttr);
    this.jobs = { n: n1, nAttr, job: c.job.view(), task: c.task.view(), flags: c.flags.view(), release: c.release.view(),
      start: c.start.view(), finish: c.finish.view(), deadline: c.deadline.view(), attrNs: c.attr.view() };
    const touched = new Set<number>();
    for (let i = n0; i < n1; i++) {
      const t = this.jobs.task[i];
      const arr = this.perTask[t];
      if (!arr) continue;
      // keep each task list sorted by release (completions can arrive slightly out of order)
      let p = arr.length;
      while (p > 0 && this.jobs.release[arr[p - 1]] > this.jobs.release[i]) p--;
      arr.splice(p, 0, i);
      touched.add(t);
      if (this.jobs.flags[i] & JOB_MISSED) this.missIndices.push(i);
    }
    for (const t of touched) this.jobsByTask[t] = Int32Array.from(this.perTask[t]);
    this.manifest.jobs.count = n1;
  }

  private ingestSeries(series: LiveBatchMsg['series']): void {
    for (const [name, s] of Object.entries(series)) {
      const vals = new Float32Array(buf(s.values));
      let g = this.seriesBufs.get(name);
      let info = this.seriesInfo.get(name);
      if (!g || !info) {
        g = new Grow(Float32Array, 4096);
        this.seriesBufs.set(name, g);
        info = { name, label: name, unit: s.unit, group: s.group, t0_ps: s.t0_ps, rate_hz: s.rate_hz, offset: 0, count: 0 };
        this.seriesInfo.set(name, info);
        this.manifest.series.items.push(info);
      }
      // place by time so a policy switch (new manager) continues the same series
      const at = Math.max(0, Math.round(((s.t0_ps - info.t0_ps) / 1e12) * info.rate_hz));
      const end = at + vals.length;
      g.reserve(end);
      if (at > g.n) g.a.fill(g.n > 0 ? g.a[g.n - 1] : vals[0], g.n, at);
      g.a.set(vals, at);
      g.n = Math.max(g.n, end);
      info.count = g.n;
      this.series.set(name, g.view());
    }
  }

  private ingestRobot(b: ArrayBuffer, events: RobotEvent[]): void {
    const r = this.manifest.robot;
    if (!r) return;
    r.events.push(...events);
    const stride = r.bodies.length * 7;
    const f = new Float32Array(b);
    const nf = Math.floor(f.length / stride);
    for (let i = 0; i < nf; i++) {
      const fi = this.frameCount + i;
      const c = Math.floor(fi / r.chunk_frames);
      let arr = this.robotChunks.get(c);
      if (!arr) { arr = new Float32Array(r.chunk_frames * stride); this.robotChunks.set(c, arr); }
      arr.set(f.subarray(i * stride, (i + 1) * stride), (fi - c * r.chunk_frames) * stride);
    }
    this.frameCount += nf;
    r.frames = this.frameCount;
  }
}

function pick(ev: EventBlock, idx: number[]): EventBlock {
  const n = idx.length;
  const out: EventBlock = {
    n, t: new Float64Array(n), dur: new Float64Array(n), kind: new Uint8Array(n), block: new Uint8Array(n), unit: new Uint16Array(n),
    task: new Uint16Array(n), src: new Uint16Array(n), dst: new Uint16Array(n), flags: new Uint16Array(n), job: new Uint32Array(n),
    op: new Uint32Array(n), bytes: new Float32Array(n), value: new Float32Array(n), aux: new Uint32Array(n),
  };
  idx.forEach((i, j) => {
    out.t[j] = ev.t[i]; out.dur[j] = ev.dur[i]; out.kind[j] = ev.kind[i]; out.block[j] = ev.block[i]; out.unit[j] = ev.unit[i];
    out.task[j] = ev.task[i]; out.src[j] = ev.src[i]; out.dst[j] = ev.dst[i]; out.flags[j] = ev.flags[i]; out.job[j] = ev.job[i];
    out.op[j] = ev.op[i]; out.bytes[j] = ev.bytes[i]; out.value[j] = ev.value[i]; out.aux[j] = ev.aux[i];
  });
  return out;
}
