// Binary decoders shared by the replay worker and the live WebSocket client.
import type { EventBlock, JobTable } from './types';

const TWO32 = 4294967296;

function i64(dv: DataView, off: number): number {
  return dv.getInt32(off + 4, true) * TWO32 + dv.getUint32(off, true);
}

export function emptyEvents(): EventBlock {
  return decodeEvents(new ArrayBuffer(0));
}

/** Decode 48-byte event records into a struct-of-arrays block. */
export function decodeEvents(buf: ArrayBuffer): EventBlock {
  const n = Math.floor(buf.byteLength / 48);
  const dv = new DataView(buf);
  const b: EventBlock = {
    n,
    t: new Float64Array(n), dur: new Float64Array(n), kind: new Uint8Array(n), block: new Uint8Array(n),
    unit: new Uint16Array(n), task: new Uint16Array(n), src: new Uint16Array(n), dst: new Uint16Array(n),
    flags: new Uint16Array(n), job: new Uint32Array(n), op: new Uint32Array(n), bytes: new Float32Array(n),
    value: new Float32Array(n), aux: new Uint32Array(n),
  };
  for (let i = 0; i < n; i++) {
    const o = i * 48;
    b.t[i] = i64(dv, o);
    b.dur[i] = i64(dv, o + 8);
    b.kind[i] = dv.getUint8(o + 16);
    b.block[i] = dv.getUint8(o + 17);
    b.unit[i] = dv.getUint16(o + 18, true);
    b.task[i] = dv.getUint16(o + 20, true);
    b.src[i] = dv.getUint16(o + 22, true);
    b.dst[i] = dv.getUint16(o + 24, true);
    b.flags[i] = dv.getUint16(o + 26, true);
    b.job[i] = dv.getUint32(o + 28, true);
    b.op[i] = dv.getUint32(o + 32, true);
    b.bytes[i] = dv.getFloat32(o + 36, true);
    b.value[i] = dv.getFloat32(o + 40, true);
    b.aux[i] = dv.getUint32(o + 44, true);
  }
  return b;
}

/** Concatenate event blocks (used when live batches arrive). */
export function concatEvents(a: EventBlock, c: EventBlock): EventBlock {
  const n = a.n + c.n;
  const cat = <T extends Float64Array | Uint8Array | Uint16Array | Uint32Array | Float32Array>(x: T, y: T, ctor: new (n: number) => T): T => {
    const r = new ctor(n);
    (r as unknown as { set(v: ArrayLike<number>, o?: number): void }).set(x, 0);
    (r as unknown as { set(v: ArrayLike<number>, o?: number): void }).set(y, a.n);
    return r;
  };
  return {
    n,
    t: cat(a.t, c.t, Float64Array), dur: cat(a.dur, c.dur, Float64Array), kind: cat(a.kind, c.kind, Uint8Array),
    block: cat(a.block, c.block, Uint8Array), unit: cat(a.unit, c.unit, Uint16Array), task: cat(a.task, c.task, Uint16Array),
    src: cat(a.src, c.src, Uint16Array), dst: cat(a.dst, c.dst, Uint16Array), flags: cat(a.flags, c.flags, Uint16Array),
    job: cat(a.job, c.job, Uint32Array), op: cat(a.op, c.op, Uint32Array), bytes: cat(a.bytes, c.bytes, Float32Array),
    value: cat(a.value, c.value, Float32Array), aux: cat(a.aux, c.aux, Uint32Array),
  };
}

/** Decode 80-byte job records. */
export function decodeJobs(buf: ArrayBuffer, nAttr: number): JobTable {
  const n = Math.floor(buf.byteLength / 80);
  const dv = new DataView(buf);
  const j: JobTable = {
    n, nAttr,
    job: new Uint32Array(n), task: new Uint16Array(n), flags: new Uint16Array(n),
    release: new Float64Array(n), start: new Float64Array(n), finish: new Float64Array(n), deadline: new Float64Array(n),
    attrNs: new Float32Array(n * nAttr),
  };
  for (let i = 0; i < n; i++) {
    const o = i * 80;
    j.job[i] = dv.getUint32(o, true);
    j.task[i] = dv.getUint16(o + 4, true);
    j.flags[i] = dv.getUint16(o + 6, true);
    j.release[i] = i64(dv, o + 8);
    j.start[i] = i64(dv, o + 16);
    j.finish[i] = i64(dv, o + 24);
    j.deadline[i] = i64(dv, o + 32);
    for (let k = 0; k < nAttr; k++) j.attrNs[i * nAttr + k] = dv.getFloat32(o + 40 + k * 4, true);
  }
  return j;
}

/** Gunzip with the platform DecompressionStream. */
export async function gunzip(buf: ArrayBuffer): Promise<ArrayBuffer> {
  const ds = new DecompressionStream('gzip');
  const stream = new Blob([buf]).stream().pipeThrough(ds);
  return await new Response(stream).arrayBuffer();
}

/** Op/xfer index lists and per-bucket NoC flow aggregates (done once per chunk, off the main thread). */
export function indexEvents(b: EventBlock, bucketPs = 4e9): EventBlock {
  const ops: number[] = [];
  const xf: number[] = [];
  const agg = new Map<number, number>();
  const at: number[] = [], as: number[] = [], ad: number[] = [], ak: number[] = [], ab: number[] = [];
  for (let i = 0; i < b.n; i++) {
    const k = b.kind[i];
    if (k === 4) ops.push(i);
    else if (k === 5) {
      xf.push(i);
      const s = b.src[i], d = b.dst[i];
      if (s === 0xffff || d === 0xffff) continue;
      // spread a long transfer over the buckets it covers
      const t0 = b.t[i], t1 = t0 + Math.max(b.dur[i], 1);
      const b0 = Math.floor(t0 / bucketPs), b1 = Math.floor((t1 - 1) / bucketPs);
      const per = b.bytes[i] / (b1 - b0 + 1);
      for (let q = b0; q <= b1; q++) {
        const key = ((q * 64 + s) * 64 + d) * 4096 + (b.task[i] & 4095);
        let j = agg.get(key);
        if (j === undefined) {
          j = at.length;
          agg.set(key, j);
          at.push(q * bucketPs); as.push(s); ad.push(d); ak.push(b.task[i]); ab.push(0);
        }
        ab[j] += per;
      }
    }
  }
  ops.sort((x, y) => b.t[x] - b.t[y]);
  b.ops = Int32Array.from(ops);
  b.xfers = Int32Array.from(xf);
  b.agg = {
    bucketPs, n: at.length, t: Float64Array.from(at), src: Uint16Array.from(as), dst: Uint16Array.from(ad),
    task: Uint16Array.from(ak), bytes: Float32Array.from(ab),
  };
  return b;
}

/** First index in sorted idx (by b.t) whose start time is >= t. */
export function lowerBound(b: EventBlock, idx: Int32Array, t: number): number {
  let lo = 0, hi = idx.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (b.t[idx[mid]] < t) lo = mid + 1; else hi = mid;
  }
  return lo;
}

export function eventBuffers(b: EventBlock): ArrayBuffer[] {
  const out = [b.t, b.dur, b.kind, b.block, b.unit, b.task, b.src, b.dst, b.flags, b.job, b.op, b.bytes, b.value, b.aux]
    .map((a) => a.buffer as ArrayBuffer);
  if (b.ops) out.push(b.ops.buffer as ArrayBuffer);
  if (b.xfers) out.push(b.xfers.buffer as ArrayBuffer);
  if (b.agg) out.push(...[b.agg.t, b.agg.src, b.agg.dst, b.agg.task, b.agg.bytes].map((a) => a.buffer as ArrayBuffer));
  return out;
}
