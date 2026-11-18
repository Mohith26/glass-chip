// Live mode: WebSocket client for tools/live_server.py (msgpack in, JSON commands out).
// Batch payloads use the replay record layouts; LiveStore decodes them with the replay decoders.
import { decode } from '@msgpack/msgpack';
import type { Manifest } from '../replay/types';
import type { LiveBatchMsg } from './liveStore';

export type LiveStatus = 'connecting' | 'open' | 'closed' | 'error';

export class LiveClient {
  private ws: WebSocket | null = null;
  onHello: ((m: Manifest, applied: Record<string, unknown>[]) => void) | null = null;
  onBatch: ((b: LiveBatchMsg) => void) | null = null;
  onStatus: ((s: LiveStatus) => void) | null = null;

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

  /** Send a command (scenario, policy, vmin, qos, pause, resume, max_rate, restart). */
  send(msg: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private handle(data: ArrayBuffer): void {
    const msg = decode(new Uint8Array(data)) as Record<string, unknown>;
    if (msg.type === 'hello') this.onHello?.(msg.manifest as Manifest, (msg.applied as Record<string, unknown>[]) ?? []);
    else if (msg.type === 'batch') this.onBatch?.(msg as unknown as LiveBatchMsg);
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}
