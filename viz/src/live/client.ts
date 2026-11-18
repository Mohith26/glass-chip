// Live mode: WebSocket client that consumes msgpack event batches from the Python server.
// The binary payloads use the same record layouts as replay chunks, so decoding is shared.
import { decode } from '@msgpack/msgpack';
import { decodeEvents, decodeJobs } from '../replay/decode';
import type { EventBlock, JobTable, Manifest } from '../replay/types';

/**
 * Message shapes (msgpack maps):
 *   {type: "hello", manifest: Manifest}                  sent once on connect (same schema as replay manifest)
 *   {type: "batch", t_ps: int, events: bin, jobs: bin, series: {name: [float, ...]}, robot: bin}
 *       events: concatenated 48-byte event records
 *       jobs:   concatenated 80-byte job records
 *       robot:  float32 [frames, bodies, 7] for frames produced since the last batch
 *   {type: "control_ack", ...}
 */
export interface LiveBatch {
  t_ps: number;
  events: EventBlock;
  jobs: JobTable | null;
  series: Record<string, number[]>;
  robot: Float32Array | null;
}

export class LiveClient {
  private ws: WebSocket | null = null;
  onHello: ((m: Manifest) => void) | null = null;
  onBatch: ((b: LiveBatch) => void) | null = null;
  onStatus: ((s: 'connecting' | 'open' | 'closed' | 'error') => void) | null = null;
  private nAttr = 10;

  connect(url: string): void {
    this.onStatus?.('connecting');
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => this.onStatus?.('open');
    ws.onclose = () => this.onStatus?.('closed');
    ws.onerror = () => this.onStatus?.('error');
    ws.onmessage = (ev: MessageEvent<ArrayBuffer>) => this.handle(ev.data);
    this.ws = ws;
  }

  /** Send a control message (scenario action, policy switch) to the live server. */
  send(msg: Record<string, unknown>): void {
    this.ws?.send(JSON.stringify(msg));
  }

  private handle(buf: ArrayBuffer): void {
    const msg = decode(new Uint8Array(buf)) as Record<string, unknown>;
    if (msg.type === 'hello') {
      const m = msg.manifest as Manifest;
      this.nAttr = m.jobs?.attribution_keys?.length ?? 10;
      this.onHello?.(m);
    } else if (msg.type === 'batch') {
      const toBuf = (u: unknown): ArrayBuffer | null => {
        if (!(u instanceof Uint8Array)) return null;
        return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
      };
      const ev = toBuf(msg.events);
      const jb = toBuf(msg.jobs);
      const rb = toBuf(msg.robot);
      this.onBatch?.({
        t_ps: Number(msg.t_ps ?? 0),
        events: decodeEvents(ev ?? new ArrayBuffer(0)),
        jobs: jb ? decodeJobs(jb, this.nAttr) : null,
        series: (msg.series as Record<string, number[]>) ?? {},
        robot: rb ? new Float32Array(rb) : null,
      });
    }
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}
