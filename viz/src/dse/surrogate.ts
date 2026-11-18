// Design-space panel backed by the conformal surrogate (ONNX, run with onnxruntime-web).
// Until a model exists, the panel renders a clearly disabled state with no outputs.

export interface SurrogateInput { name: string; label: string; unit?: string; min: number; max: number; step?: number; default: number }
export interface SurrogateOutput { name: string; label: string; unit?: string; lo?: string; hi?: string }
export interface SurrogateMeta {
  model: string;
  input_name?: string;
  inputs: SurrogateInput[];
  outputs: SurrogateOutput[];
  coverage?: number;
  pareto?: { x: string; y: string; points?: number[][] };
  provenance?: Record<string, unknown>;
}

type OrtModule = typeof import('onnxruntime-web/wasm');
type Session = Awaited<ReturnType<OrtModule['InferenceSession']['create']>>;

const PLACEHOLDER_INPUTS: SurrogateInput[] = [
  { name: 'npu_tiles', label: 'NPU tiles', min: 2, max: 8, step: 1, default: 4 },
  { name: 'npu_array', label: 'NPU array size', min: 32, max: 128, step: 32, default: 64 },
  { name: 'dram_gbps', label: 'DRAM bandwidth', unit: 'GB/s', min: 64, max: 204, step: 4, default: 136 },
  { name: 'cache_mb', label: 'System cache', unit: 'MB', min: 2, max: 16, step: 1, default: 8 },
  { name: 'vmin_npu', label: 'NPU Vmin', unit: 'V', min: 0.45, max: 0.8, step: 0.01, default: 0.45 },
];

export class DsePanel {
  private session: Session | null = null;
  private meta: SurrogateMeta | null = null;
  private values: Record<string, number> = {};
  private ort: OrtModule | null = null;

  constructor(private host: HTMLElement) {}

  async init(metaUrl: string | null): Promise<void> {
    if (!metaUrl) { this.renderDisabled('No surrogate is listed in replays/index.json (key "surrogate.meta").'); return; }
    try {
      const r = await fetch(metaUrl);
      if (!r.ok) throw new Error(`metadata ${r.status}`);
      const meta = (await r.json()) as SurrogateMeta;
      const modelUrl = new URL(meta.model, new URL(metaUrl, location.href)).href;
      const head = await fetch(modelUrl, { method: 'HEAD' });
      if (!head.ok) throw new Error(`model file missing (${head.status})`);
      this.ort = await import('onnxruntime-web/wasm');
      this.session = await this.ort.InferenceSession.create(modelUrl, { executionProviders: ['wasm'] });
      this.meta = meta;
      for (const i of meta.inputs) this.values[i.name] = i.default;
      this.renderLive();
      await this.run();
    } catch (e) {
      this.renderDisabled(`Surrogate not loaded: ${String((e as Error).message ?? e)}.`);
    }
  }

  private renderDisabled(reason: string): void {
    this.host.innerHTML = `
      <section class="card disabled">
        <header><h3>Design space</h3><span class="pill pill-off">Surrogate not loaded</span></header>
        <p class="note">${reason} The sliders will run the conformal surrogate in the browser once surrogate/export_onnx.py publishes a model. Nothing below is computed until then.</p>
        ${PLACEHOLDER_INPUTS.map((i) => `
          <label class="slider-row is-disabled"><span>${i.label}</span>
            <input type="range" min="${i.min}" max="${i.max}" step="${i.step ?? 1}" value="${i.default}" disabled />
            <span class="slider-val">–</span></label>`).join('')}
        <div class="pareto-empty">Pareto view: energy per robot-hour vs worst safety-task miss rate. Waiting for the surrogate.</div>
      </section>`;
  }

  private renderLive(): void {
    const m = this.meta!;
    this.host.innerHTML = `
      <section class="card">
        <header><h3>Design space</h3><span class="pill pill-on">Surrogate${m.coverage ? `, ${Math.round(m.coverage * 100)}% intervals` : ''}</span></header>
        ${m.inputs.map((i) => `
          <label class="slider-row"><span>${i.label}</span>
            <input type="range" data-in="${i.name}" min="${i.min}" max="${i.max}" step="${i.step ?? (i.max - i.min) / 100}" value="${i.default}" />
            <span class="slider-val" data-val="${i.name}">${i.default}${i.unit ? ' ' + i.unit : ''}</span></label>`).join('')}
        <div class="dse-out">${m.outputs.map((o) => `<div class="dse-row"><span>${o.label}</span><span data-out="${o.name}">–</span></div>`).join('')}</div>
        <div class="foot" data-ms></div>
      </section>`;
    this.host.querySelectorAll<HTMLInputElement>('input[data-in]').forEach((el) => {
      el.addEventListener('input', () => {
        const name = el.dataset.in!;
        this.values[name] = Number(el.value);
        const inp = m.inputs.find((x) => x.name === name);
        const v = this.host.querySelector(`[data-val="${name}"]`);
        if (v) v.textContent = `${el.value}${inp?.unit ? ' ' + inp.unit : ''}`;
        void this.run();
      });
    });
  }

  private async run(): Promise<void> {
    if (!this.session || !this.meta || !this.ort) return;
    const m = this.meta;
    const x = Float32Array.from(m.inputs.map((i) => this.values[i.name]));
    const t0 = performance.now();
    const feeds = { [m.input_name ?? this.session.inputNames[0]]: new this.ort.Tensor('float32', x, [1, x.length]) };
    const out = await this.session.run(feeds);
    const ms = performance.now() - t0;
    const get = (n?: string): number | null => {
      if (!n || !out[n]) return null;
      return Number((out[n].data as Float32Array)[0]);
    };
    for (const o of m.outputs) {
      const el = this.host.querySelector(`[data-out="${o.name}"]`);
      if (!el) continue;
      const v = get(o.name), lo = get(o.lo), hi = get(o.hi);
      el.textContent = v == null ? '–' : `${v.toFixed(3)}${lo != null && hi != null ? ` [${lo.toFixed(3)}, ${hi.toFixed(3)}]` : ''}${o.unit ? ' ' + o.unit : ''}`;
    }
    const f = this.host.querySelector('[data-ms]');
    if (f) f.textContent = `Inference ${ms.toFixed(1)} ms in this browser.`;
  }
}
