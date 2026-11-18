// Design-space panel backed by the conformal surrogate (ONNX, run with onnxruntime-web).
// Until a model exists, the panel renders a clearly disabled state with no outputs.

export interface SurrogateInput {
  name: string; label: string; unit?: string; min: number; max: number; step?: number; default: number;
  /** Dotted path into the SoC config (e.g. "blocks.npu.tiles"), used when a full run is requested. */
  config_path?: string;
  /** Categorical inputs: numbers = allowed values; strings = labels for indices 0..n-1. */
  choices?: (number | string)[];
}
export interface SurrogateOutput {
  name: string; label: string; unit?: string;
  /** Output names holding the conformal interval bounds for this output. */
  lo?: string; hi?: string;
  /** Interval width above which the result is flagged "uncertain, run full model". */
  max_width?: number;
  digits?: number;
  test_coverage?: number;
  test_coverage_ci95?: [number, number];
  method?: string;
}
export interface SurrogateMeta {
  model: string;
  input_name?: string;
  inputs: SurrogateInput[];
  outputs: SurrogateOutput[];
  coverage?: number;
  /** Interval width relative to |value| above which an output is flagged uncertain (default 0.25). */
  uncertain_rel_width?: number;
  pareto?: { x: string; y: string; x_label?: string; y_label?: string; points?: number[][]; source?: string; point_fields?: string[] };
  provenance?: Record<string, unknown>;
  fallback_rule?: { latency?: string; miss_rate?: string };
  guarantee?: string;
  labels?: string[];
  coverage_achieved?: { min?: number; median?: number; max?: number };
  recommended?: { name: string; inputs: Record<string, number> }[];
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

function isCat(i: SurrogateInput): boolean {
  return !!i.choices?.length && typeof i.choices[0] === 'string';
}
function pct(v?: number): string {
  return v == null ? 'n/a' : `${(v * 100).toFixed(1)}%`;
}
function prettyName(n: string): string {
  const s = n.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function setPath(obj: Record<string, number>, path: string, v: number): void {
  obj[path] = v;
}

export class DsePanel {
  private session: Session | null = null;
  get loaded(): boolean { return this.session !== null; }
  private meta: SurrogateMeta | null = null;
  private values: Record<string, number> = {};
  private ort: OrtModule | null = null;
  private lastOut: Record<string, number | null> = {};
  /** Live mode hook: request a full simulation for the current slider point. */
  onRunFull: ((overrides: Record<string, number>, values: Record<string, number>) => void) | null = null;
  liveAvailable = false;
  /** Task deadlines (ms) used by the latency fallback rule, from the loaded replay. */
  deadlinesMs: Record<string, number> = {};

  constructor(private host: HTMLElement) {}

  async init(metaUrl: string | null): Promise<void> {
    if (!metaUrl) { this.renderDisabled('No surrogate model has been published to surrogate/meta.json yet.'); return; }
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
        ${m.recommended?.length ? `<div class="chips dse-presets">${m.recommended.map((r, k) => `<button class="chip" data-rec="${k}">${prettyName(r.name)}</button>`).join('')}<button class="chip" data-rec="-1">Defaults</button></div>` : ''}
        ${m.inputs.map((i) => {
          const cat = isCat(i);
          const lo = cat ? 0 : i.min, hi = cat ? i.choices!.length - 1 : i.max, st = cat ? 1 : (i.step ?? (i.max - i.min) / 100);
          return `
          <label class="slider-row"><span>${i.label}</span>
            <input type="range" data-in="${i.name}" min="${lo}" max="${hi}" step="${st}" value="${i.default}" />
            <span class="slider-val" data-val="${i.name}">${this.fmtIn(i, i.default)}</span></label>`;
        }).join('')}
        <div class="dse-out">${m.outputs.map((o) => `<div class="dse-row" data-row="${o.name}"><span>${o.label}</span><span class="dse-val" data-out="${o.name}">–</span></div>`).join('')}</div>
        <div class="dse-uncertain" data-unc hidden>
          <div><b>Uncertain, run full model.</b> <span data-unc-why></span></div>
          <button class="btn small" data-runfull>Run full model</button>
        </div>
        ${m.pareto ? `<canvas class="pareto" height="200"></canvas><div class="foot">${m.pareto.x_label ?? ''}; ${m.pareto.y_label ?? ''}. The frontier covers all training workloads; this design is shown for the selected workload.</div>` : ''}
        <div class="foot" data-ms></div>
        ${m.guarantee ? `<div class="foot"><b>What the intervals guarantee:</b> ${m.guarantee}</div>` : ''}
        ${m.coverage_achieved ? `<div class="foot">Held-out coverage per output ranges from ${pct(m.coverage_achieved.min)} to ${pct(m.coverage_achieved.max)} (median ${pct(m.coverage_achieved.median)}).</div>` : ''}
        ${m.labels?.length ? `<ul class="dse-labels">${m.labels.map((l) => `<li>${l}</li>`).join('')}</ul>` : ''}
      </section>`;
    this.host.querySelectorAll<HTMLButtonElement>('[data-rec]').forEach((b) => b.addEventListener('click', () => {
      const k = Number(b.dataset.rec);
      for (const i of m.inputs) this.values[i.name] = k < 0 ? i.default : (m.recommended![k].inputs[i.name] ?? i.default);
      this.syncSliders();
      void this.run();
    }));
    this.host.querySelector('[data-runfull]')?.addEventListener('click', () => this.runFull());
    this.host.querySelectorAll<HTMLInputElement>('input[data-in]').forEach((el) => {
      el.addEventListener('input', () => {
        const name = el.dataset.in!;
        const inp0 = m.inputs.find((x) => x.name === name);
        let v = Number(el.value);
        if (inp0 && !isCat(inp0) && inp0.choices?.length) {
          const nums = inp0.choices.map(Number);
          v = nums.reduce((a, b) => (Math.abs(b - v) < Math.abs(a - v) ? b : a));
        }
        this.values[name] = v;
        const lab = this.host.querySelector(`[data-val="${name}"]`);
        if (lab && inp0) lab.textContent = this.fmtIn(inp0, v);
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
    const rel = m.uncertain_rel_width ?? 0.25;
    const unc: string[] = [];
    this.lastOut = {};
    for (const o of m.outputs) {
      const el = this.host.querySelector(`[data-out="${o.name}"]`);
      const row = this.host.querySelector(`[data-row="${o.name}"]`);
      const v = get(o.name), lo = get(o.lo), hi = get(o.hi);
      this.lastOut[o.name] = v;
      if (o.lo) this.lastOut[o.lo] = lo;
      if (o.hi) this.lastOut[o.hi] = hi;
      const d = o.digits ?? 2;
      if (el) el.innerHTML = v == null ? '–' : `<b>${v.toFixed(d)}</b>${lo != null && hi != null ? ` <small>[${lo.toFixed(d)}, ${hi.toFixed(d)}]</small>` : ''}${o.unit ? ` <small>${o.unit}</small>` : ''}`;
      let wide = false;
      if (v != null && lo != null && hi != null) {
        const w = hi - lo;
        const lim = this.widthLimit(o);
        wide = lim != null ? w > lim : m.uncertain_rel_width != null ? w > rel * Math.max(Math.abs(v), 1e-9) : false;
        if (wide) unc.push(o.label);
      }
      if (row && o.test_coverage != null) (row as HTMLElement).title = `${o.method ?? 'conformal'}: held-out coverage ${pct(o.test_coverage)}${o.test_coverage_ci95 ? ` (95% CI ${pct(o.test_coverage_ci95[0])} to ${pct(o.test_coverage_ci95[1])})` : ''}`;
      row?.classList.toggle('wide', wide);
    }
    const ub = this.host.querySelector('[data-unc]') as HTMLElement | null;
    if (ub) {
      ub.hidden = unc.length === 0;
      const why = ub.querySelector('[data-unc-why]');
      const wired = m.inputs.some((i) => i.config_path);
      if (why) why.textContent = `The ${Math.round((m.coverage ?? 0.9) * 100)}% interval is wide for ${unc.join(', ')}.` +
        (!wired ? ' Confirm this point with a full simulation run before relying on it.' : this.liveAvailable ? '' : ' Start live mode to run this point through the full simulator.');
      const btn = ub.querySelector('[data-runfull]') as HTMLButtonElement | null;
      if (btn) { btn.disabled = !this.liveAvailable || !wired; btn.title = wired ? '' : 'This model does not map its inputs to SoC config paths yet'; }
    }
    this.drawPareto();
    const f = this.host.querySelector('[data-ms]');
    if (f) f.textContent = `Inference ${ms.toFixed(1)} ms in this browser${m.coverage ? `; intervals are split-conformal at ${Math.round(m.coverage * 100)}% nominal coverage` : ''}.`;
  }

  /** Fallback rule from the metadata: latency width > 0.5 x deadline, miss-rate width > 0.05. */
  private widthLimit(o: SurrogateOutput): number | null {
    if (o.max_width != null) return o.max_width;
    const fr = this.meta?.fallback_rule;
    if (!fr) return null;
    if (/miss_rate$/.test(o.name) && fr.miss_rate) {
      const mm = /> *([0-9.]+)/.exec(fr.miss_rate);
      return mm ? Number(mm[1]) : null;
    }
    const lat = /^(T\w+?)__p99_ms$/.exec(o.name);
    if (lat && fr.latency) {
      const mm = /> *([0-9.]+) *x *deadline/.exec(fr.latency);
      const d = this.deadlinesMs[lat[1]];
      return mm && d ? Number(mm[1]) * d : null;
    }
    return null;
  }

  private fmtIn(i: SurrogateInput, v: number): string {
    if (isCat(i)) return String(i.choices![Math.round(v)] ?? v);
    const d = (i.step ?? 1) < 1 ? 2 : 0;
    return `${Number(v).toFixed(d)}${i.unit ? ' ' + i.unit : ''}`;
  }

  private syncSliders(): void {
    const m = this.meta!;
    for (const i of m.inputs) {
      const el = this.host.querySelector(`input[data-in="${i.name}"]`) as HTMLInputElement | null;
      if (el) el.value = String(this.values[i.name]);
      const lab = this.host.querySelector(`[data-val="${i.name}"]`);
      if (lab) lab.textContent = this.fmtIn(i, this.values[i.name]);
    }
  }

  private runFull(): void {
    if (!this.meta || !this.onRunFull) return;
    const ov: Record<string, number> = {};
    for (const i of this.meta.inputs) if (i.config_path) setPath(ov, i.config_path, this.values[i.name]);
    this.onRunFull(ov, { ...this.values });
  }

  private drawPareto(): void {
    const m = this.meta;
    const c = this.host.querySelector('canvas.pareto') as HTMLCanvasElement | null;
    if (!m?.pareto || !c) return;
    const p = m.pareto;
    const w = c.clientWidth, h = 200, dpr = Math.min(devicePixelRatio, 2);
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const g = c.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const pts = p.points ?? [];
    const xo = m.outputs.find((o) => o.name === p.x), yo = m.outputs.find((o) => o.name === p.y);
    // the frontier's y axis is an upper bound, so place this design at its upper bound too
    const yUpper = /upper/.test(p.point_fields?.[1] ?? '') || /upper bound/.test(p.y_label ?? '');
    const cx = this.lastOut[p.x], cy = yUpper ? (this.lastOut[yo?.hi ?? ''] ?? this.lastOut[p.y]) : this.lastOut[p.y];
    const xs = [...pts.map((q) => q[0]), ...(cx != null ? [cx, this.lastOut[xo?.lo ?? ''] ?? cx, this.lastOut[xo?.hi ?? ''] ?? cx] : [])];
    const ys = [...pts.map((q) => q[1]), ...(cy != null ? [cy, this.lastOut[yo?.lo ?? ''] ?? cy, this.lastOut[yo?.hi ?? ''] ?? cy] : [])];
    if (!xs.length) return;
    const pad = { l: 40, r: 10, t: 24, b: 30 };
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const X = (v: number): number => pad.l + ((v - x0) / Math.max(1e-9, x1 - x0)) * (w - pad.l - pad.r);
    const Y = (v: number): number => h - pad.b - ((v - y0) / Math.max(1e-9, y1 - y0)) * (h - pad.t - pad.b);
    g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillStyle = '#dfe6f0';
    g.fillText(`${xo?.label ?? p.x}${xo?.unit ? ` (${xo.unit})` : ''}`, pad.l, h - 8);
    g.save(); g.translate(11, h - pad.b); g.rotate(-Math.PI / 2); g.fillText(`${yo?.label ?? p.y}${yUpper ? ', upper bound' : ''}`, 0, 0); g.restore();
    g.fillStyle = 'rgba(160, 182, 220, 0.75)';
    for (const q of pts) { g.beginPath(); g.arc(X(q[0]), Y(q[1]), 2.5, 0, Math.PI * 2); g.fill(); }
    if (cx != null && cy != null) {
      const xl = this.lastOut[xo?.lo ?? ''], xh = this.lastOut[xo?.hi ?? ''], yl = this.lastOut[yo?.lo ?? ''], yh = this.lastOut[yo?.hi ?? ''];
      g.strokeStyle = '#ffffff';
      g.lineWidth = 1.5;
      if (xl != null && xh != null) { g.beginPath(); g.moveTo(X(xl), Y(cy)); g.lineTo(X(xh), Y(cy)); g.stroke(); }
      if (yl != null && yh != null && !yUpper) { g.beginPath(); g.moveTo(X(cx), Y(yl)); g.lineTo(X(cx), Y(yh)); g.stroke(); }
      g.fillStyle = '#ffffff';
      g.beginPath(); g.arc(X(cx), Y(cy), 5, 0, Math.PI * 2); g.fill();
    }
    // legend on its own line above the plot so it never sits on the points
    g.fillStyle = '#ffffff';
    g.beginPath(); g.arc(pad.l + 4, 8, 4, 0, Math.PI * 2); g.fill();
    g.fillText('this design', pad.l + 12, 12);
    g.fillStyle = 'rgba(160, 182, 220, 0.9)';
    g.beginPath(); g.arc(pad.l + 90, 8, 2.5, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#d3dbe6';
    g.fillText(`frontier, ${pts.length} designs`, pad.l + 97, 12);
  }
}
