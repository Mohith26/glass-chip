// Web Worker: fetch, gunzip and decode replay chunks off the main thread.
import { decodeEvents, decodeJobs, eventBuffers, gunzip, indexEvents } from './decode';

type Req =
  | { id: number; type: 'events'; url: string }
  | { id: number; type: 'jobs'; url: string; nAttr: number }
  | { id: number; type: 'raw'; url: string; gz: boolean }
  | { id: number; type: 'json'; url: string; gz: boolean };

const ctx = self as unknown as DedicatedWorkerGlobalScope;

async function load(url: string, gz: boolean): Promise<ArrayBuffer> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  const b = await r.arrayBuffer();
  // Some static hosts transparently decompress .gz with Content-Encoding; detect the gzip magic.
  const u = new Uint8Array(b, 0, Math.min(2, b.byteLength));
  if (gz && u.length === 2 && u[0] === 0x1f && u[1] === 0x8b) return gunzip(b);
  return b;
}

ctx.onmessage = async (ev: MessageEvent<Req>) => {
  const m = ev.data;
  try {
    if (m.type === 'events') {
      const blk = indexEvents(decodeEvents(await load(m.url, true)));
      ctx.postMessage({ id: m.id, ok: true, data: blk }, eventBuffers(blk));
    } else if (m.type === 'jobs') {
      const j = decodeJobs(await load(m.url, true), m.nAttr);
      ctx.postMessage({ id: m.id, ok: true, data: j }, [j.job.buffer, j.task.buffer, j.flags.buffer, j.release.buffer,
        j.start.buffer, j.finish.buffer, j.deadline.buffer, j.attrNs.buffer] as ArrayBuffer[]);
    } else if (m.type === 'raw') {
      const b = await load(m.url, m.gz);
      ctx.postMessage({ id: m.id, ok: true, data: b }, [b]);
    } else {
      const b = await load(m.url, m.gz);
      ctx.postMessage({ id: m.id, ok: true, data: JSON.parse(new TextDecoder().decode(b)) });
    }
  } catch (e) {
    ctx.postMessage({ id: m.id, ok: false, error: String(e) });
  }
};
