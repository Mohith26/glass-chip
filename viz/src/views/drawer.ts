// Right drawer: deadlines, power, temperature, latency tolerance, design-space panel.
import { fmtNum } from '../clock';
import { MISS_RED, taskColor } from '../palette';
import type { ReplayStore } from '../replay/store';
import { SHORT } from './rings';

const DOMAIN_LABEL: Record<string, string> = {
  cpu_a: 'CPU A', cpu_b: 'CPU B', rt: 'RT', npu: 'NPU', simt: 'SIMT', dsp: 'DSP', noc_cache: 'NoC + cache', dram_io: 'DRAM I/O',
};

interface S14Point { latency_ms: number; grasp_success_rate: number; grasp_success_ci95: [number, number]; runs: number }
interface S14 {
  sweep: Record<string, { points: S14Point[] }>;
  baseline: Record<string, { grasp_success_rate: number; grasp_success_ci95: [number, number] }>;
  summary: { grasp_tolerance_ms: Record<string, number> };
  assumptions?: { policy?: string };
  provenance?: { script?: string; seeds?: number[]; duration_s?: number; chip_models?: string[] };
}

let s14Cache: Promise<S14 | null> | null = null;
function loadS14(): Promise<S14 | null> {
  if (!s14Cache) s14Cache = fetch('results/s14.json').then((r) => (r.ok ? (r.json() as Promise<S14>) : null)).catch(() => null);
  return s14Cache;
}

export class Drawer {
  private store: ReplayStore | null = null;
  private live = new Map<string, HTMLElement>();
  private sparkCanvas: HTMLCanvasElement | null = null;
  private tolCanvas: HTMLCanvasElement | null = null;
  private totalPower: Float32Array | null = null;
  private s14: S14 | null = null;
  private tolSpeed = '0.2';
  onJumpMiss: ((dir: 1 | -1) => void) | null = null;

  constructor(private host: HTMLElement, private dseHost: HTMLElement) {}

  setStore(store: ReplayStore): void {
    this.store = store;
    const m = store.manifest;
    // whole-run power numbers: the power manager's own summary when the replay carries it
    const psum = (m as unknown as { power_summary?: { avg_power_w?: number; peak_power_w?: number; energy_per_robot_hour_wh?: number; peak_temp_c?: number } }).power_summary;
    const r = { ...m.results, ...(psum ? { avg_power_w: psum.avg_power_w, peak_power_w: psum.peak_power_w,
      energy_per_robot_hour_wh: psum.energy_per_robot_hour_wh, peak_temp_c: psum.peak_temp_c } : {}) };
    const doms = Object.keys(m.soc.power_domains);
    // total power series for the sparkline
    const ps = doms.map((d) => store.series.get(`power.${d}`)).filter((x): x is Float32Array => !!x);
    if (ps.length) {
      const n = Math.min(...ps.map((p) => p.length));
      const tot = new Float32Array(n);
      for (const p of ps) for (let i = 0; i < n; i++) tot[i] += p[i];
      this.totalPower = tot;
    } else this.totalPower = null;

    const taskRows = m.tasks.filter((t) => t.deadline_us).map((t) => {
      const tr = r.tasks?.[t.id];
      if (!tr || !tr.jobs) return '';
      const p99 = tr.p99_ms ?? NaN;
      const dl = (t.deadline_us ?? 0) / 1000;
      const frac = Math.min(1.25, p99 / dl);
      const bad = tr.misses > 0;
      return `<div class="kv-row">
        <span class="dot" style="background:${taskColor(t.id, t.color)}"></span>
        <span class="kv-name">${SHORT[t.id] ?? t.name}</span>
        <span class="bar"><span class="bar-fill" style="width:${(Math.min(1, frac) * 100).toFixed(1)}%;background:${bad ? MISS_RED : taskColor(t.id, t.color)}"></span><span class="bar-dl"></span></span>
        <span class="kv-num">${Number.isFinite(p99) ? fmtNum(p99, p99 >= 10 ? 1 : 2) : '–'}<small> / ${fmtNum(dl, dl >= 10 ? 0 : 1)} ms</small></span>
        <span class="kv-num ${bad ? 'bad' : ''}">${tr.misses}<small> / ${tr.jobs.toLocaleString('en-US')}</small></span>
      </div>`;
    }).join('');

    this.host.innerHTML = `
      <section class="card">
        <header><h3>Deadlines</h3><span class="hint">p99 response vs deadline, misses / jobs, whole run</span></header>
        <div class="kv-head"><span></span><span>Task</span><span></span><span>p99</span><span>Misses</span></div>
        ${taskRows}
        <div class="row-actions">
          <button class="btn small" data-act="prev-miss">‹ Previous miss</button>
          <button class="btn small" data-act="next-miss">Next miss ›</button>
          <span class="muted-strong">${store.missIndices.length.toLocaleString('en-US')} missed jobs</span>
        </div>
      </section>
      <section class="card">
        <header><h3>Power</h3><span class="hint">now, at the playhead</span></header>
        <div class="big-row">
          <div class="big"><div class="big-num" data-live="ptotal">–</div><div class="big-label">SoC power</div></div>
          <div class="big"><div class="big-num">${r.energy_per_robot_hour_wh != null ? fmtNum(r.energy_per_robot_hour_wh, 2) : '–'}<small> Wh</small></div><div class="big-label">Energy per robot-hour</div></div>
          <div class="big"><div class="big-num" data-live="tmax">–</div><div class="big-label">Hottest domain</div></div>
        </div>
        <canvas class="spark" height="54"></canvas>
        <div class="domains">${doms.map((d) => `
          <div class="dom-row"><span class="dom-name">${DOMAIN_LABEL[d] ?? d}</span>
            <span class="dom-v" data-live="v.${d}">–</span><span class="dom-f" data-live="f.${d}">–</span>
            <span class="dom-p" data-live="p.${d}">–</span></div>`).join('')}
        </div>
        <div class="foot">Run average ${r.avg_power_w != null ? fmtNum(r.avg_power_w, 2) + ' W' : '–'}, peak ${r.peak_power_w != null ? fmtNum(r.peak_power_w, 2) + ' W' : '–'}, peak temperature ${r.peak_temp_c != null ? fmtNum(r.peak_temp_c, 1) + ' °C' : '–'}.</div>
      </section>
      <section class="card" id="tol-card">
        <header><h3>Latency tolerance</h3><span class="hint">grasp success vs injected camera-to-grasp latency</span></header>
        <div class="chips tol-speeds" data-speeds></div>
        <canvas class="tol" height="150"></canvas>
        <div class="foot" data-tolfoot>Loading results/s14.json…</div>
      </section>`;
    this.live.clear();
    this.host.querySelectorAll<HTMLElement>('[data-live]').forEach((el) => this.live.set(el.dataset.live!, el));
    this.sparkCanvas = this.host.querySelector('canvas.spark');
    const spd = Number((m as unknown as { conveyor_speed_mps?: number }).conveyor_speed_mps ?? NaN);
    if (Number.isFinite(spd)) this.tolSpeed = String(spd);
    void loadS14().then((d) => { this.s14 = d; this.renderTolMeta(); });
    this.tolCanvas = this.host.querySelector('canvas.tol');
    this.host.querySelector('[data-act="next-miss"]')?.addEventListener('click', () => this.onJumpMiss?.(1));
    this.host.querySelector('[data-act="prev-miss"]')?.addEventListener('click', () => this.onJumpMiss?.(-1));
    void this.dseHost;
  }

  private renderTolMeta(): void {
    const foot = this.host.querySelector('[data-tolfoot]') as HTMLElement | null;
    const chips = this.host.querySelector('[data-speeds]') as HTMLElement | null;
    const d = this.s14;
    if (!foot || !chips) return;
    if (!d) { foot.textContent = 'results/s14.json is not available, so no tolerance band is shown.'; chips.innerHTML = ''; return; }
    const speeds = Object.keys(d.sweep).sort();
    if (!speeds.includes(this.tolSpeed)) this.tolSpeed = speeds.includes('0.2') ? '0.2' : speeds[0];
    chips.innerHTML = speeds.map((sp) => `<button class="chip ${sp === this.tolSpeed ? 'on' : ''}" data-sp="${sp}">${sp} m/s</button>`).join('');
    chips.querySelectorAll<HTMLButtonElement>('[data-sp]').forEach((b) => b.addEventListener('click', () => {
      this.tolSpeed = b.dataset.sp!;
      this.renderTolMeta();
    }));
    const tol = d.summary.grasp_tolerance_ms[this.tolSpeed];
    const lat = d.sweep[this.tolSpeed].points.map((p) => p.latency_ms);
    const maxLat = Math.max(...lat);
    const runs = d.sweep[this.tolSpeed].points[0]?.runs;
    const seeds = d.provenance?.seeds?.length;
    const dur = d.provenance?.duration_s;
    foot.innerHTML = `${tol >= maxLat ? `At ${this.tolSpeed} m/s success stays within 90% of baseline across the whole sweep (up to ${maxLat} ms).`
      : `At ${this.tolSpeed} m/s success stays within 90% of baseline up to ${tol} ms (shaded).`}
      Shaded bands are 95% intervals over ${runs ?? '?'} runs${seeds ? ` (${seeds} seeds` : ''}${dur ? `, ${dur} s each)` : seeds ? ')' : ''}.
      <b>Hybrid mode:</b> a scripted expert drives the arm and latency is injected, so this is the robot-side requirement, not a chip result. Source: ${d.provenance?.script ?? 'results/s14.json'}.`;
  }

  update(t: number): void {
    const st = this.store;
    if (!st) return;
    const doms = Object.keys(st.manifest.soc.power_domains);
    let tot = 0, tmax = -Infinity, tdom = '';
    for (const d of doms) {
      const p = st.sample(`power.${d}`, t);
      const v = st.sample(`volt.${d}`, t);
      const f = st.sample(`freq.${d}`, t);
      const T = st.sample(`temp.${d}`, t);
      if (Number.isFinite(p)) tot += p;
      if (Number.isFinite(T) && T > tmax) { tmax = T; tdom = d; }
      this.set(`v.${d}`, Number.isFinite(v) ? `${v.toFixed(2)} V` : '–');
      this.set(`f.${d}`, Number.isFinite(f) ? `${(f / 1e9).toFixed(2)} GHz` : '–');
      this.set(`p.${d}`, Number.isFinite(p) ? `${p.toFixed(2)} W` : '–');
    }
    this.set('ptotal', `${tot.toFixed(2)}<small> W</small>`, true);
    this.set('tmax', Number.isFinite(tmax) ? `${tmax.toFixed(1)}<small> °C ${DOMAIN_LABEL[tdom] ?? tdom}</small>` : '–', true);
    this.drawSpark(t);
    this.drawTol(t);
  }

  private set(k: string, v: string, html = false): void {
    const el = this.live.get(k);
    if (!el) return;
    if (html) { if (el.innerHTML !== v) el.innerHTML = v; } else if (el.textContent !== v) el.textContent = v;
  }

  private drawSpark(t: number): void {
    const c = this.sparkCanvas, p = this.totalPower, st = this.store;
    if (!c || !p || !st) return;
    const w = c.clientWidth, h = 54, dpr = Math.min(devicePixelRatio, 2);
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const g = c.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    let mx = 0;
    for (let i = 0; i < p.length; i++) mx = Math.max(mx, p[i]);
    mx = mx * 1.1 || 1;
    const step = Math.max(1, Math.floor(p.length / w));
    g.beginPath();
    g.moveTo(0, h);
    for (let x = 0; x < w; x++) {
      const i0 = Math.floor((x / w) * p.length);
      let s = 0, n = 0;
      for (let i = i0; i < Math.min(p.length, i0 + step); i++) { s += p[i]; n++; }
      g.lineTo(x, h - 2 - ((n ? s / n : 0) / mx) * (h - 6));
    }
    g.lineTo(w, h);
    g.closePath();
    const grd = g.createLinearGradient(0, 0, 0, h);
    grd.addColorStop(0, 'rgba(255,190,90,0.55)');
    grd.addColorStop(1, 'rgba(255,190,90,0.04)');
    g.fillStyle = grd;
    g.fill();
    const x = (t / st.duration) * w;
    g.fillStyle = '#ffffff';
    g.fillRect(x - 0.75, 0, 1.5, h);
    g.fillStyle = '#f4f7fb';
    g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillText(`${mx.toFixed(1)} W`, 4, 12);
  }

  private drawTol(t: number): void {
    const c = this.tolCanvas, st = this.store, d = this.s14;
    if (!c || !st || !d) return;
    const w = c.clientWidth, h = 150, dpr = Math.min(devicePixelRatio, 2);
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const g = c.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const pad = { l: 36, r: 10, t: 10, b: 22 };
    const lmax = 400, ymin = 0.5;
    const X = (l: number): number => pad.l + (l / lmax) * (w - pad.l - pad.r);
    const Y = (v: number): number => pad.t + (1 - (v - ymin) / (1 - ymin)) * (h - pad.t - pad.b);
    g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
    g.strokeStyle = '#2a3446';
    g.lineWidth = 1;
    for (const v of [0.5, 0.75, 1.0]) {
      g.beginPath(); g.moveTo(pad.l, Y(v)); g.lineTo(w - pad.r, Y(v)); g.stroke();
      g.fillStyle = '#dfe6f0';
      g.fillText(`${Math.round(v * 100)}%`, 2, Y(v) + 4);
    }
    for (const l of [0, 100, 200, 300, 400]) {
      g.fillStyle = '#dfe6f0';
      g.fillText(l === 400 ? '400 ms' : String(l), Math.min(X(l) - 4, w - 44), h - 6);
    }
    const sel = this.tolSpeed;
    const tol = d.summary.grasp_tolerance_ms[sel];
    if (tol !== undefined) {
      g.fillStyle = 'rgba(61,220,151,0.13)';
      g.fillRect(X(0), pad.t, X(Math.min(tol, lmax)) - X(0), h - pad.t - pad.b);
    }
    const speeds = Object.keys(d.sweep).sort();
    // end-of-curve labels for the other speeds, nudged apart so they never overlap
    const labels = speeds.filter((sp) => sp !== sel).map((sp) => {
      const pts = d.sweep[sp].points;
      const last = pts[pts.length - 1];
      return { sp, y: Y(Math.max(ymin, last.grasp_success_rate)) - 4 };
    }).sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 11) labels[i].y = labels[i - 1].y + 11;
    for (const sp of speeds) {
      const pts = [{ latency_ms: 0, grasp_success_rate: d.baseline[sp].grasp_success_rate, grasp_success_ci95: d.baseline[sp].grasp_success_ci95 },
        ...d.sweep[sp].points];
      const on = sp === sel;
      if (on) {
        g.fillStyle = 'rgba(61,220,151,0.22)';
        g.beginPath();
        pts.forEach((p, i) => (i ? g.lineTo(X(p.latency_ms), Y(Math.max(ymin, p.grasp_success_ci95[1]))) : g.moveTo(X(p.latency_ms), Y(Math.max(ymin, p.grasp_success_ci95[1])))));
        for (let i = pts.length - 1; i >= 0; i--) g.lineTo(X(pts[i].latency_ms), Y(Math.max(ymin, pts[i].grasp_success_ci95[0])));
        g.closePath();
        g.fill();
      }
      g.strokeStyle = on ? '#3ddc97' : 'rgba(210,220,235,0.55)';
      g.lineWidth = on ? 2.2 : 1.2;
      g.beginPath();
      pts.forEach((p, i) => (i ? g.lineTo(X(p.latency_ms), Y(Math.max(ymin, p.grasp_success_rate))) : g.moveTo(X(p.latency_ms), Y(Math.max(ymin, p.grasp_success_rate)))));
      g.stroke();
      if (!on) {
        const lab = labels.find((l) => l.sp === sp)!;
        g.fillStyle = '#d3dbe6';
        g.fillText(`${sp} m/s`, w - pad.r - 44, lab.y);
      }
    }
    // marker: this replay's measured camera-to-grasp latency (median), when the replay carries it
    const rm = (st.manifest as unknown as { robot_metrics?: { chain_latency_ms?: { camera_to_grasp?: { p50?: number | null } } } }).robot_metrics;
    const e2e = rm?.chain_latency_ms?.camera_to_grasp?.p50;
    if (e2e != null && Number.isFinite(e2e)) {
      const x = X(Math.min(lmax, e2e));
      g.fillStyle = '#ffffff';
      g.fillRect(x - 1, pad.t, 2, h - pad.t - pad.b);
      g.fillText(`this run ${e2e.toFixed(0)} ms (p50)`, Math.min(w - 120, x + 5), pad.t + 12);
    }
    void t;
  }
}
