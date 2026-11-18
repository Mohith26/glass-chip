// Right drawer: deadlines, power, temperature, latency tolerance, design-space panel.
import { fmtNum } from '../clock';
import { MISS_RED, taskColor } from '../palette';
import type { ReplayStore } from '../replay/store';
import { SHORT, fmtMs } from './rings';

const DOMAIN_LABEL: Record<string, string> = {
  cpu_a: 'CPU A', cpu_b: 'CPU B', rt: 'RT', npu: 'NPU', simt: 'SIMT', dsp: 'DSP', noc_cache: 'NoC + cache', dram_io: 'DRAM I/O',
};

export class Drawer {
  private store: ReplayStore | null = null;
  private live = new Map<string, HTMLElement>();
  private sparkCanvas: HTMLCanvasElement | null = null;
  private tolCanvas: HTMLCanvasElement | null = null;
  private totalPower: Float32Array | null = null;
  onJumpMiss: ((dir: 1 | -1) => void) | null = null;

  constructor(private host: HTMLElement, private dseHost: HTMLElement) {}

  setStore(store: ReplayStore): void {
    this.store = store;
    const m = store.manifest;
    const r = m.results;
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

    const tol = m.latency_tolerance;
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
      <section class="card">
        <header><h3>Latency tolerance</h3><span class="hint">grasp success vs end-to-end latency</span></header>
        ${tol ? `<canvas class="tol" height="120"></canvas><div class="foot">${tol.synthetic ? '<b class="warn">Synthetic placeholder curve.</b> ' : ''}${tol.note ?? ''} Marker: latest VLA response.</div>` : '<div class="empty">No latency-tolerance data in this replay.</div>'}
      </section>`;
    this.live.clear();
    this.host.querySelectorAll<HTMLElement>('[data-live]').forEach((el) => this.live.set(el.dataset.live!, el));
    this.sparkCanvas = this.host.querySelector('canvas.spark');
    this.tolCanvas = this.host.querySelector('canvas.tol');
    this.host.querySelector('[data-act="next-miss"]')?.addEventListener('click', () => this.onJumpMiss?.(1));
    this.host.querySelector('[data-act="prev-miss"]')?.addEventListener('click', () => this.onJumpMiss?.(-1));
    void this.dseHost;
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
    const c = this.tolCanvas, st = this.store;
    const tol = st?.manifest.latency_tolerance;
    if (!c || !st || !tol) return;
    const w = c.clientWidth, h = 120, dpr = Math.min(devicePixelRatio, 2);
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const g = c.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const L = tol.latency_ms, S = tol.success;
    const lmax = Math.max(...L);
    const pad = { l: 34, r: 8, t: 8, b: 20 };
    const X = (l: number): number => pad.l + (l / lmax) * (w - pad.l - pad.r);
    const Y = (s: number): number => pad.t + (1 - s) * (h - pad.t - pad.b);
    // tolerance band: latency where success stays above 95%
    let ok = L[0];
    for (let i = 0; i < L.length; i++) if (S[i] >= 0.95) ok = L[i];
    g.fillStyle = 'rgba(61,220,151,0.14)';
    g.fillRect(X(0), pad.t, X(ok) - X(0), h - pad.t - pad.b);
    g.strokeStyle = '#3ddc97';
    g.lineWidth = 2;
    g.beginPath();
    L.forEach((l, i) => (i ? g.lineTo(X(l), Y(S[i])) : g.moveTo(X(l), Y(S[i]))));
    g.stroke();
    g.fillStyle = '#e8edf4';
    g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillText('100%', 2, Y(1) + 4);
    g.fillText('0%', 12, Y(0));
    g.fillText(`${lmax} ms`, w - 46, h - 4);
    g.fillText('0', pad.l, h - 4);
    g.fillText(`≥95% up to ${ok} ms`, X(0) + 6, pad.t + 12);
    // marker: latest completed VLA job response
    const ti = st.manifest.tasks.findIndex((x) => x.id === 'T4_vla');
    if (ti >= 0) {
      let ji = st.latestJob(ti, t);
      while (ji >= 0 && st.jobs.finish[ji] > t) {
        const arr = st.jobsByTask[ti];
        const pos = arr.indexOf(ji);
        ji = pos > 0 ? arr[pos - 1] : -1;
      }
      if (ji >= 0) {
        const resp = st.jobs.finish[ji] - st.jobs.release[ji];
        const x = X(Math.min(lmax, resp / 1e9));
        g.fillStyle = (st.jobs.flags[ji] & 1) ? MISS_RED : '#ffffff';
        g.fillRect(x - 1, pad.t, 2, h - pad.t - pad.b);
        g.fillText(`VLA ${fmtMs(resp)}`, Math.min(w - 70, x + 4), h - pad.b - 4);
      }
    }
  }
}
