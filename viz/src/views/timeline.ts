// Perfetto-style timeline on a 2D canvas: overview brush, time axis, task tracks, engine tracks.
import { fmtTime } from '../clock';
import { MISS_RED, taskColor, taskRank } from '../palette';
import type { ReplayStore } from '../replay/store';
import { KIND } from '../replay/types';
import { lowerBound } from '../replay/decode';
import type { CritOp } from '../replay/types';
import { blockLabel } from './die';
import { SHORT } from './rings';

const GUTTER = 168;
const OVERVIEW_H = 30;
const AXIS_H = 24;
const ROW_TASK = 20;
const ROW_UNIT = 18;
const GROUP_H = 22;
const DETAIL_SPAN = 1.2e12; // draw individual ops when the view is narrower than this

type Row =
  | { kind: 'group'; label: string; y: number; h: number; key: string }
  | { kind: 'task'; task: number; y: number; h: number }
  | { kind: 'block'; block: number; units: number[]; y: number; h: number; expanded: boolean; id: string }
  | { kind: 'unit'; block: number; unit: number; lodIndex: number; y: number; h: number; id: string };

export interface Selection { jobIndex: number; crit: CritOp[] | null }

export class Timeline {
  private ctx: CanvasRenderingContext2D;
  private w = 0;
  private h = 0;
  private dpr = 1;
  view0 = 0;
  view1 = 1;
  private anim: { a0: number; a1: number; b0: number; b1: number; t0: number; ms: number } | null = null;
  private store: ReplayStore | null = null;
  private rows: Row[] = [];
  private scrollY = 0;
  private contentH = 0;
  private expanded = new Set<string>(['npu']);
  private collapsedGroups = new Set<string>();
  private drag: { x: number; y: number; v0: number; v1: number; mode: 'pan' | 'scrub' | 'brush'; moved: boolean } | null = null;
  private lastInteract = 0;
  selection: Selection | null = null;
  private hoverX = -1;
  private hoverY = -1;
  follow = true;
  onSeek: ((t: number) => void) | null = null;
  onUserScrub: (() => void) | null = null;
  onSelectJob: ((jobIndex: number) => void) | null = null;
  private colors: string[] = [];
  private ovCache: Float32Array | null = null;
  private ovKey = '';

  constructor(private canvas: HTMLCanvasElement) {
    this.ctx = canvas.getContext('2d')!;
    canvas.addEventListener('wheel', (e) => this.wheel(e), { passive: false });
    canvas.addEventListener('pointerdown', (e) => this.down(e));
    window.addEventListener('pointermove', (e) => this.move(e));
    window.addEventListener('pointerup', (e) => this.up(e));
    canvas.addEventListener('pointerleave', () => { this.hoverX = -1; });
    window.addEventListener('keydown', (e) => this.key(e));
  }

  setStore(store: ReplayStore): void {
    this.store = store;
    this.colors = store.manifest.tasks.map((t) => taskColor(t.id, t.color));
    const live = !!(store.manifest as unknown as { live?: boolean }).live;
    if (live) {
      this.view0 = 0;
      this.view1 = 3e12;
    } else if (this.view1 <= 1 || this.view1 > store.duration) {
      this.view0 = 0;
      this.view1 = Math.min(store.duration, 3e12);
    }
    this.selection = null;
  }

  resize(w: number, h: number): void {
    this.dpr = Math.min(devicePixelRatio, 2);
    this.w = w; this.h = h;
    this.canvas.width = Math.round(w * this.dpr);
    this.canvas.height = Math.round(h * this.dpr);
  }

  setView(a: number, b: number, animateMs = 0): void {
    const st = this.store;
    const live = !!(st?.manifest as unknown as { live?: boolean } | undefined)?.live;
    // live runs grow, so the view may extend past the data received so far
    const dur = st ? (live ? Math.max(st.duration, b) : st.duration) : 1;
    const span = Math.max(2e5, Math.min(dur, b - a));
    let a2 = Math.max(0, a), b2 = a2 + span;
    if (b2 > dur) { b2 = dur; a2 = Math.max(0, dur - span); }
    if (animateMs > 0) this.anim = { a0: this.view0, a1: this.view1, b0: a2, b1: b2, t0: performance.now(), ms: animateMs };
    else { this.anim = null; this.view0 = a2; this.view1 = b2; }
  }

  zoomToJob(i: number): void {
    const st = this.store!;
    const j = st.jobView(i);
    const end = Math.max(j.finish, j.deadline);
    const span = Math.max(end - j.release, 1e8);
    this.setView(j.release - span * 0.25, end + span * 0.35, 650);
    this.lastInteract = performance.now();
  }

  private x(t: number): number {
    return GUTTER + ((t - this.view0) / (this.view1 - this.view0)) * (this.w - GUTTER - 8);
  }
  private tAt(x: number): number {
    return this.view0 + ((x - GUTTER) / (this.w - GUTTER - 8)) * (this.view1 - this.view0);
  }

  private buildRows(): void {
    const st = this.store!;
    const m = st.manifest;
    const rows: Row[] = [];
    let y = 0;
    rows.push({ kind: 'group', label: 'Robot tasks', y, h: GROUP_H, key: 'tasks' });
    y += GROUP_H;
    if (!this.collapsedGroups.has('tasks')) {
      const order = m.tasks.map((t, i) => [t.id, i] as const).sort((a, b) => taskRank(a[0]) - taskRank(b[0]));
      for (const [, i] of order) {
        if (st.jobsByTask[i]?.length) { rows.push({ kind: 'task', task: i, y, h: ROW_TASK }); y += ROW_TASK; }
      }
    }
    rows.push({ kind: 'group', label: 'Engines', y, h: GROUP_H, key: 'engines' });
    y += GROUP_H;
    if (!this.collapsedGroups.has('engines')) {
      const byBlock = new Map<number, number[]>();
      m.lod.units.forEach((u, ui) => {
        if (u.block === 'dram') return;
        const a = byBlock.get(u.block_index) ?? [];
        a.push(ui);
        byBlock.set(u.block_index, a);
      });
      for (const [bi, uis] of byBlock) {
        const id = m.soc.blocks[bi].id;
        const ex = this.expanded.has(id);
        rows.push({ kind: 'block', block: bi, units: uis, y, h: ROW_UNIT + 2, expanded: ex, id });
        y += ROW_UNIT + 2;
        if (ex) {
          for (const ui of uis) {
            rows.push({ kind: 'unit', block: bi, unit: m.lod.units[ui].unit, lodIndex: ui, y, h: ROW_UNIT, id });
            y += ROW_UNIT;
          }
        }
      }
    }
    this.rows = rows;
    this.contentH = y;
  }

  private trackTop(): number { return OVERVIEW_H + AXIS_H; }

  // ------------------------------------------------------------ interaction
  private wheel(e: WheelEvent): void {
    e.preventDefault();
    this.lastInteract = performance.now();
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    const span = this.view1 - this.view0;
    if (e.ctrlKey || e.metaKey || (my < this.trackTop() && Math.abs(e.deltaY) > Math.abs(e.deltaX))) {
      const k = Math.exp(e.deltaY * (e.ctrlKey ? 0.012 : 0.0025));
      const anchor = mx > GUTTER ? this.tAt(mx) : (this.view0 + this.view1) / 2;
      this.setView(anchor - (anchor - this.view0) * k, anchor + (this.view1 - anchor) * k);
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
      const d = (e.shiftKey ? e.deltaY : e.deltaX) / (this.w - GUTTER);
      this.setView(this.view0 + d * span, this.view1 + d * span);
    } else {
      const max = Math.max(0, this.contentH - (this.h - this.trackTop()));
      this.scrollY = Math.max(0, Math.min(max, this.scrollY + e.deltaY));
    }
  }

  private down(e: PointerEvent): void {
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    this.lastInteract = performance.now();
    if (my < OVERVIEW_H) {
      const st = this.store;
      if (!st) return;
      const t = ((mx - GUTTER) / (this.w - GUTTER - 8)) * st.duration;
      const span = this.view1 - this.view0;
      this.setView(t - span / 2, t + span / 2);
      this.onSeek?.(Math.max(0, Math.min(st.duration, t)));
      this.drag = { x: mx, y: my, v0: this.view0, v1: this.view1, mode: 'brush', moved: false };
      return;
    }
    if (my < this.trackTop() && mx > GUTTER) {
      this.drag = { x: mx, y: my, v0: this.view0, v1: this.view1, mode: 'scrub', moved: false };
      this.onSeek?.(this.tAt(mx));
      return;
    }
    this.drag = { x: mx, y: my, v0: this.view0, v1: this.view1, mode: 'pan', moved: false };
    this.canvas.setPointerCapture(e.pointerId);
  }

  private move(e: PointerEvent): void {
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    this.hoverX = mx; this.hoverY = my;
    const d = this.drag;
    if (!d) {
      this.canvas.style.cursor = my < this.trackTop() && mx > GUTTER ? 'col-resize' : mx < GUTTER ? 'pointer' : 'grab';
      return;
    }
    if (Math.abs(mx - d.x) + Math.abs(my - d.y) > 3) d.moved = true;
    this.lastInteract = performance.now();
    if (d.mode === 'pan') {
      const dt = ((mx - d.x) / (this.w - GUTTER - 8)) * (d.v1 - d.v0);
      this.setView(d.v0 - dt, d.v1 - dt);
      const max = Math.max(0, this.contentH - (this.h - this.trackTop()));
      this.scrollY = Math.max(0, Math.min(max, this.scrollY - e.movementY));
      this.canvas.style.cursor = 'grabbing';
    } else if (d.mode === 'scrub') {
      this.onSeek?.(Math.max(this.view0, Math.min(this.view1, this.tAt(mx))));
    } else if (d.mode === 'brush' && this.store) {
      const t = ((mx - GUTTER) / (this.w - GUTTER - 8)) * this.store.duration;
      const span = this.view1 - this.view0;
      this.setView(t - span / 2, t + span / 2);
      this.onSeek?.(Math.max(0, Math.min(this.store.duration, t)));
    }
  }

  private up(e: PointerEvent): void {
    const d = this.drag;
    this.drag = null;
    if (!d || d.moved || d.mode !== 'pan') return;
    const r = this.canvas.getBoundingClientRect();
    this.click(e.clientX - r.left, e.clientY - r.top);
  }

  private rowAt(my: number): Row | undefined {
    const y = my - this.trackTop() + this.scrollY;
    return this.rows.find((r) => y >= r.y && y < r.y + r.h);
  }

  private click(mx: number, my: number): void {
    const row = this.rowAt(my);
    if (!row || !this.store) return;
    if (mx < GUTTER || row.kind === 'group') {
      if (row.kind === 'group') {
        if (this.collapsedGroups.has(row.key)) this.collapsedGroups.delete(row.key); else this.collapsedGroups.add(row.key);
      } else if (row.kind === 'block') {
        if (this.expanded.has(row.id)) this.expanded.delete(row.id); else this.expanded.add(row.id);
      }
      return;
    }
    const t = this.tAt(mx);
    const tol = ((this.view1 - this.view0) / (this.w - GUTTER)) * 3;
    const st = this.store;
    if (row.kind === 'task') {
      const arr = st.jobsByTask[row.task];
      let best = -1;
      for (let p = st.firstJobPos(row.task, t - 2e11); p < arr.length; p++) {
        const ji = arr[p];
        const r0 = st.jobs.release[ji];
        if (r0 > t + tol) break;
        const fin = st.jobs.finish[ji] >= 0 ? st.jobs.finish[ji] : st.jobs.deadline[ji];
        if (t >= r0 - tol && t <= fin + tol) best = ji;
      }
      if (best >= 0) this.onSelectJob?.(best);
      else this.onSeek?.(t);
      return;
    }
    if (row.kind === 'unit' || row.kind === 'block') {
      for (const b of st.loadedBlocks(t - 1e11, t + 1e11)) {
        for (let i = 0; i < b.n; i++) {
          if (b.kind[i] !== KIND.op || b.block[i] !== row.block) continue;
          if (row.kind === 'unit' && b.unit[i] !== row.unit) continue;
          if (t >= b.t[i] - tol && t <= b.t[i] + b.dur[i] + tol) {
            const ji = st.findJobById(b.job[i]);
            if (ji >= 0) { this.onSelectJob?.(ji); return; }
          }
        }
      }
      this.onSeek?.(t);
    }
  }

  private key(e: KeyboardEvent): void {
    if ((e.target as HTMLElement)?.closest('input, textarea, select')) return;
    const span = this.view1 - this.view0;
    const c = (this.view0 + this.view1) / 2;
    if (e.key === 'w') this.setView(c - span * 0.35, c + span * 0.35, 180);
    else if (e.key === 's') this.setView(c - span * 0.7, c + span * 0.7, 180);
    else if (e.key === 'a') this.setView(this.view0 - span * 0.25, this.view1 - span * 0.25, 180);
    else if (e.key === 'd') this.setView(this.view0 + span * 0.25, this.view1 + span * 0.25, 180);
    else return;
    this.lastInteract = performance.now();
  }

  // ------------------------------------------------------------ drawing
  draw(t: number, playing: boolean): void {
    const st = this.store;
    const g = this.ctx;
    if (!st || this.w === 0) return;
    if (this.anim) {
      const k = Math.min(1, (performance.now() - this.anim.t0) / this.anim.ms);
      const e = 1 - Math.pow(1 - k, 4);
      this.view0 = this.anim.a0 + (this.anim.b0 - this.anim.a0) * e;
      this.view1 = this.anim.a1 + (this.anim.b1 - this.anim.a1) * e;
      if (k >= 1) this.anim = null;
    } else if (this.follow && playing && performance.now() - this.lastInteract > 2500 && !this.drag) {
      const span = this.view1 - this.view0;
      if (t > this.view1 - span * 0.04 || t < this.view0) this.setView(t - span * 0.12, t + span * 0.88);
    }
    this.buildRows();
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.fillStyle = '#0b0e14';
    g.fillRect(0, 0, this.w, this.h);
    g.font = '500 12px "Inter Variable", Inter, system-ui, sans-serif';
    g.textBaseline = 'middle';

    this.drawTracks(t);
    this.drawOverview(t);
    this.drawAxis(t);
  }

  private drawOverview(t: number): void {
    const st = this.store!;
    const g = this.ctx;
    const W = this.w - GUTTER - 8;
    const X = (tt: number): number => GUTTER + (tt / st.duration) * W;
    g.fillStyle = '#0b0e14';
    g.fillRect(0, 0, this.w, OVERVIEW_H);
    g.fillStyle = '#121824';
    g.fillRect(GUTTER, 6, W, OVERVIEW_H - 10);
    // total busy (LOD) as a faint area, cached per store and width (it only changes in live mode)
    const m = st.manifest;
    const key = `${W}|${m.lod.bins}|${st.version}`;
    if (!this.ovCache || this.ovKey !== key) {
      this.ovKey = key;
      const nu = m.lod.units.length;
      const bins = m.lod.bins;
      const step = Math.max(1, Math.floor(bins / W));
      const hs = new Float32Array(Math.max(1, Math.floor(W)));
      for (let px = 0; px < hs.length; px++) {
        const b0 = Math.floor((px / W) * bins);
        let s = 0, c = 0;
        for (let b = b0; b < Math.min(bins, b0 + step); b++) for (let u = 0; u < nu; u++) { s += st.lodBusy[b * nu + u]; c++; }
        hs[px] = c ? s / c / 255 : 0;
      }
      let mx = 0;
      for (const v of hs) mx = Math.max(mx, v);
      this.ovCache = hs.map((v) => (mx > 0 ? v / mx : 0));
    }
    g.fillStyle = 'rgba(125, 160, 220, 0.4)';
    const hs = this.ovCache;
    for (let px = 0; px < hs.length; px++) {
      const hh = hs[px] * (OVERVIEW_H - 12);
      g.fillRect(GUTTER + px, OVERVIEW_H - 5 - hh, 1, hh);
    }
    g.fillStyle = MISS_RED;
    for (const ji of st.missIndices) {
      const x = X(st.jobs.deadline[ji]);
      g.fillRect(x - 0.75, 6, 1.5, OVERVIEW_H - 10);
    }
    for (const se of m.scenario_events ?? []) {
      const x = X(se.t_s * 1e12);
      g.fillStyle = '#ffffff';
      g.beginPath(); g.moveTo(x, 4); g.lineTo(x + 4, 9); g.lineTo(x, 14); g.lineTo(x - 4, 9); g.fill();
    }
    const bx0 = X(this.view0), bx1 = X(this.view1);
    g.fillStyle = 'rgba(255,255,255,0.10)';
    g.fillRect(bx0, 4, Math.max(2, bx1 - bx0), OVERVIEW_H - 6);
    g.strokeStyle = 'rgba(255,255,255,0.75)';
    g.lineWidth = 1;
    g.strokeRect(bx0 + 0.5, 4.5, Math.max(2, bx1 - bx0) - 1, OVERVIEW_H - 7);
    g.fillStyle = '#ffffff';
    g.fillRect(X(t) - 1, 3, 2, OVERVIEW_H - 4);
    g.fillStyle = '#f4f7fb';
    g.font = '600 12px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillText('Full run', 14, OVERVIEW_H / 2 + 1);
    g.fillStyle = '#c9d3e0';
    g.font = '500 12px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillText(fmtTime(st.duration), 76, OVERVIEW_H / 2 + 1);
  }

  private drawAxis(t: number): void {
    const g = this.ctx;
    const y0 = OVERVIEW_H;
    g.fillStyle = '#0f131b';
    g.fillRect(0, y0, this.w, AXIS_H);
    g.fillStyle = '#1d2533';
    g.fillRect(0, y0 + AXIS_H - 1, this.w, 1);
    const span = this.view1 - this.view0;
    const target = span / Math.max(1, (this.w - GUTTER) / 110);
    const p10 = Math.pow(10, Math.floor(Math.log10(target)));
    const stepT = [1, 2, 5, 10].map((k) => k * p10).find((s) => s >= target) ?? p10 * 10;
    g.fillStyle = '#dfe6f0';
    g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
    g.save();
    g.beginPath(); g.rect(GUTTER, y0, this.w - GUTTER, AXIS_H); g.clip();
    for (let tt = Math.ceil(this.view0 / stepT) * stepT; tt <= this.view1; tt += stepT) {
      const x = this.x(tt);
      g.fillStyle = '#2a3446';
      g.fillRect(x, y0 + AXIS_H - 7, 1, 6);
      g.fillStyle = '#dfe6f0';
      g.fillText(fmtTime(tt), x + 4, y0 + AXIS_H / 2);
    }
    g.restore();
    // playhead pill
    const px = this.x(t);
    if (px >= GUTTER && px <= this.w) {
      const label = fmtTime(t);
      g.font = '600 11px "Inter Variable", Inter, system-ui, sans-serif';
      const tw = g.measureText(label).width + 14;
      const lx = Math.min(this.w - tw - 4, Math.max(GUTTER, px - tw / 2));
      g.fillStyle = '#ffffff';
      roundRect(g, lx, y0 + 3, tw, AXIS_H - 6, 9);
      g.fill();
      g.fillStyle = '#0b0e14';
      g.fillText(label, lx + 7, y0 + AXIS_H / 2);
    }
    g.fillStyle = '#0f131b';
    g.fillRect(0, y0, GUTTER, AXIS_H);
    g.fillStyle = '#f4f7fb';
    g.font = '600 12px "Inter Variable", Inter, system-ui, sans-serif';
    g.fillText(`Window  ${fmtTime(span)}`, 14, y0 + AXIS_H / 2);
  }

  private drawTracks(t: number): void {
    const st = this.store!;
    const g = this.ctx;
    const top = this.trackTop();
    const m = st.manifest;
    const pxPerPs = (this.w - GUTTER - 8) / (this.view1 - this.view0);
    const detail = this.view1 - this.view0 <= DETAIL_SPAN;
    if (detail) void st.ensureEvents(this.view0, this.view1);
    const sel = this.selection;
    const selJob = sel ? st.jobs.job[sel.jobIndex] : -1;
    const critKeys = new Set<string>();
    if (sel?.crit) for (const c of sel.crit) critKeys.add(c.op_id + '@' + c.engine);
    const blocks = detail ? st.loadedBlocks(this.view0, this.view1) : [];

    g.save();
    g.beginPath(); g.rect(0, top, this.w, this.h - top); g.clip();
    const rowRects = new Map<string, { y: number; h: number }>();
    for (const row of this.rows) {
      const y = top + row.y - this.scrollY;
      if (y + row.h < top || y > this.h) continue;
      if (row.kind === 'group') {
        g.fillStyle = '#111722';
        g.fillRect(0, y, this.w, row.h);
        g.fillStyle = '#f4f7fb';
        g.font = '650 12px "Inter Variable", Inter, system-ui, sans-serif';
        const col = this.collapsedGroups.has(row.key);
        g.fillText(`${col ? '▸' : '▾'}  ${row.label}`, 12, y + row.h / 2 + 1);
        continue;
      }
      // zebra + separator
      g.fillStyle = (row.y / 2) % 2 < 1 ? '#0c1018' : '#0e131c';
      g.fillRect(0, y, this.w, row.h);
      g.fillStyle = '#161d29';
      g.fillRect(GUTTER, y + row.h - 1, this.w - GUTTER, 1);
      g.font = '500 12px "Inter Variable", Inter, system-ui, sans-serif';
      if (row.kind === 'task') {
        const task = m.tasks[row.task];
        g.fillStyle = this.colors[row.task];
        roundRect(g, 14, y + row.h / 2 - 4, 8, 8, 2); g.fill();
        g.fillStyle = '#eef2f7';
        g.fillText(SHORT[task.id] ?? task.name, 30, y + row.h / 2 + 1);
        this.clipTrack(y, row.h, () => this.drawJobs(row.task, y, row.h, pxPerPs, selJob));
      } else if (row.kind === 'block') {
        g.fillStyle = '#eef2f7';
        g.font = '600 12px "Inter Variable", Inter, system-ui, sans-serif';
        g.fillText(`${row.expanded ? '▾' : '▸'}  ${blockLabel(row.id)}`, 12, y + row.h / 2 + 1);
        g.font = '500 11px "Inter Variable", Inter, system-ui, sans-serif';
        g.fillStyle = '#c3cddb';
        g.fillText(`${row.units.length}`, GUTTER - 26, y + row.h / 2 + 1);
        rowRects.set(`b${row.block}`, { y, h: row.h });
        this.clipTrack(y, row.h, () => {
          if (detail) this.drawOps(blocks, row.block, -1, y, row.h, pxPerPs, selJob, critKeys, true);
          else this.drawBusy(row.units, y, row.h);
        });
      } else if (row.kind === 'unit') {
        g.fillStyle = '#dde4ee';
        g.fillText(`${blockLabel(row.id)} ${row.id === 'npu' ? 'tile' : row.id === 'simt' ? 'CU' : row.id === 'dma' ? 'engine' : 'core'} ${row.unit}`, 30, y + row.h / 2 + 1);
        rowRects.set(`u${row.block}:${row.unit}`, { y, h: row.h });
        this.clipTrack(y, row.h, () => {
          if (detail) this.drawOps(blocks, row.block, row.unit, y, row.h, pxPerPs, selJob, critKeys, false);
          else this.drawBusy([row.lodIndex], y, row.h);
        });
      }
    }
    // critical-path flow lines for the selected job
    if (sel?.crit && sel.crit.length > 1) {
      g.beginPath(); g.rect(GUTTER, top, this.w - GUTTER, this.h - top); g.clip();
      g.strokeStyle = 'rgba(255,255,255,0.85)';
      g.lineWidth = 1.5;
      let prev: { x: number; y: number } | null = null;
      for (const c of sel.crit) {
        const mm = /^([a-z_]+?)(\d+)$/.exec(c.engine);
        const bi = m.soc.blocks.findIndex((b) => b.id === (mm ? mm[1] : c.engine));
        const rr = rowRects.get(`u${bi}:${mm ? mm[2] : 0}`) ?? rowRects.get(`b${bi}`);
        if (!rr) continue;
        const p0 = { x: this.x(c.start_ps), y: rr.y + rr.h / 2 };
        if (prev) {
          g.beginPath();
          g.moveTo(prev.x, prev.y);
          g.bezierCurveTo(prev.x + 12, prev.y, p0.x - 12, p0.y, p0.x, p0.y);
          g.stroke();
        }
        prev = { x: this.x(c.end_ps), y: rr.y + rr.h / 2 };
      }
    }
    g.restore();
    // gutter divider and playhead
    g.fillStyle = '#1d2533';
    g.fillRect(GUTTER - 1, top, 1, this.h - top);
    const px = this.x(t);
    if (px >= GUTTER) {
      g.fillStyle = 'rgba(255,255,255,0.92)';
      g.fillRect(px - 0.75, top, 1.5, this.h - top);
    }
    if (this.hoverX > GUTTER && this.hoverY > top && !this.drag) {
      g.fillStyle = 'rgba(255,255,255,0.25)';
      g.fillRect(this.hoverX, top, 1, this.h - top);
    }
    // scrollbar
    const vis = this.h - top;
    if (this.contentH > vis) {
      const hh = (vis / this.contentH) * vis;
      const yy = top + (this.scrollY / this.contentH) * vis;
      g.fillStyle = 'rgba(255,255,255,0.28)';
      roundRect(g, this.w - 5, yy, 3, hh, 1.5); g.fill();
    }
  }

  private clipTrack(y: number, h: number, fn: () => void): void {
    const g = this.ctx;
    g.save();
    g.beginPath();
    g.rect(GUTTER, y, this.w - GUTTER, h);
    g.clip();
    fn();
    g.restore();
  }

  private drawJobs(task: number, y: number, h: number, pxPerPs: number, selJob: number): void {
    const st = this.store!;
    const g = this.ctx;
    const arr = st.jobsByTask[task];
    const col = this.colors[task];
    let lastPx = -10;
    const maxResp = 4e11;
    const dimmed = selJob >= 0;
    for (let p = st.firstJobPos(task, this.view0 - maxResp); p < arr.length; p++) {
      const ji = arr[p];
      const r0 = st.jobs.release[ji];
      if (r0 > this.view1) break;
      const s0 = st.jobs.start[ji] >= 0 ? st.jobs.start[ji] : r0;
      const f0 = st.jobs.finish[ji] >= 0 ? st.jobs.finish[ji] : st.jobs.deadline[ji];
      if (f0 < this.view0) continue;
      const missed = (st.jobs.flags[ji] & 1) !== 0;
      const x0 = this.x(r0), xs = this.x(s0), x1 = this.x(f0);
      const isSel = st.jobs.job[ji] === selJob;
      if (x1 - x0 < 1.2 && !missed && !isSel) {
        if (Math.floor(x0) === lastPx) continue;
        lastPx = Math.floor(x0);
        g.globalAlpha = dimmed ? 0.35 : 0.85;
        g.fillStyle = col;
        g.fillRect(x0, y + 4, 1, h - 8);
        g.globalAlpha = 1;
        continue;
      }
      g.globalAlpha = dimmed && !isSel ? 0.35 : 1;
      if (xs > x0 + 1) {
        g.fillStyle = col;
        g.globalAlpha *= 0.3;
        g.fillRect(x0, y + 6, xs - x0, h - 12);
        g.globalAlpha = dimmed && !isSel ? 0.35 : 1;
      }
      g.fillStyle = missed ? MISS_RED : col;
      roundRect(g, xs, y + 3, Math.max(1.5, x1 - xs), h - 6, 3);
      g.fill();
      if (x1 - xs > 54 && (!dimmed || isSel)) {
        g.fillStyle = '#0b0e14';
        g.font = '600 11px "Inter Variable", Inter, system-ui, sans-serif';
        g.fillText(`${((f0 - r0) / 1e9).toFixed(1)} ms`, xs + 6, y + h / 2 + 1);
      }
      // deadline tick
      const xd = this.x(st.jobs.deadline[ji]);
      if (xd - x0 > 6 && pxPerPs > 0) {
        g.fillStyle = missed ? MISS_RED : 'rgba(255,255,255,0.6)';
        g.fillRect(xd - 0.5, y + 2, 1, h - 4);
      }
      if (isSel) {
        g.strokeStyle = '#ffffff';
        g.lineWidth = 2;
        roundRect(g, xs - 1, y + 2, Math.max(3, x1 - xs + 2), h - 4, 4);
        g.stroke();
      }
      g.globalAlpha = 1;
    }
  }

  private drawOps(blocks: ReturnType<ReplayStore['loadedBlocks']>, block: number, unit: number, y: number, h: number,
    _pxPerPs: number, selJob: number, crit: Set<string>, merged: boolean): void {
    const g = this.ctx;
    const st = this.store!;
    const strings = st.manifest.events.strings;
    const id = st.manifest.soc.blocks[block].id;
    const dimmed = selJob >= 0;
    let last = -10;
    for (const b of blocks) {
      const oi = b.ops;
      if (!oi) continue;
      const j0 = lowerBound(b, oi, this.view0 - 1e11);
      for (let j = j0; j < oi.length; j++) {
        const i = oi[j];
        if (b.t[i] > this.view1) break;
        if (b.block[i] !== block) continue;
        if (unit >= 0 && b.unit[i] !== unit) continue;
        const s = b.t[i], e = s + b.dur[i];
        if (e < this.view0) continue;
        const x0 = this.x(s), x1 = this.x(e);
        const isSel = b.job[i] === selJob;
        if (x1 - x0 < 1 && !isSel) {
          if (Math.floor(x0) === last) continue;
          last = Math.floor(x0);
        }
        const yy = merged ? y + 3 + (b.unit[i] % 4) * ((h - 6) / 4) : y + 3;
        const hh = merged ? Math.max(2, (h - 6) / 4 - 0.5) : h - 6;
        g.globalAlpha = dimmed && !isSel ? 0.28 : 1;
        g.fillStyle = (b.flags[i] & 2) && isSel ? MISS_RED : this.colors[b.task[i]] ?? '#c8d2de';
        g.fillRect(x0, yy, Math.max(1, x1 - x0 - 0.5), hh);
        if (isSel) {
          const name = b.op[i] < strings.length ? strings[b.op[i]] : '';
          if (crit.has(`${name}@${id}${b.unit[i]}`) || crit.size === 0) {
            g.strokeStyle = '#ffffff';
            g.lineWidth = 1.5;
            g.strokeRect(x0 - 0.5, yy - 0.5, Math.max(2, x1 - x0), hh + 1);
          }
        }
        if (!merged && x1 - x0 > 70 && b.op[i] < strings.length && (!dimmed || isSel)) {
          g.fillStyle = '#0b0e14';
          g.font = '600 11px "Inter Variable", Inter, system-ui, sans-serif';
          g.save();
          g.beginPath(); g.rect(x0, yy, x1 - x0 - 4, hh); g.clip();
          g.fillText(strings[b.op[i]], x0 + 5, yy + hh / 2 + 1);
          g.restore();
        }
        g.globalAlpha = 1;
      }
    }
  }

  private drawBusy(units: number[], y: number, h: number): void {
    const st = this.store!;
    const g = this.ctx;
    const m = st.manifest;
    const nu = m.lod.units.length;
    const W = this.w - GUTTER - 8;
    const b0 = this.view0 / m.lod.bin_ps, b1 = this.view1 / m.lod.bin_ps;
    g.fillStyle = 'rgba(140, 175, 235, 0.85)';
    g.beginPath();
    g.moveTo(GUTTER, y + h - 2);
    for (let px = 0; px <= W; px += 1) {
      const ba = Math.floor(b0 + ((b1 - b0) * px) / W);
      const bb = Math.max(ba + 1, Math.floor(b0 + ((b1 - b0) * (px + 1)) / W));
      let s = 0, c = 0;
      for (let b = Math.max(0, ba); b < Math.min(m.lod.bins, bb); b++) for (const u of units) { s += st.lodBusy[b * nu + u]; c++; }
      const v = c ? s / c / 255 : 0;
      g.lineTo(GUTTER + px, y + h - 2 - v * (h - 5));
    }
    g.lineTo(GUTTER + W, y + h - 2);
    g.closePath();
    g.fill();
  }
}

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const rr = Math.min(r, w / 2, h / 2);
  g.beginPath();
  g.moveTo(x + rr, y);
  g.arcTo(x + w, y, x + w, y + h, rr);
  g.arcTo(x + w, y + h, x, y + h, rr);
  g.arcTo(x, y + h, x, y, rr);
  g.arcTo(x, y, x + w, y, rr);
  g.closePath();
}
