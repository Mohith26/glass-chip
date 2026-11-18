// Deadline rings around the die: one per task with a deadline.
import { MISS_RED, taskColor, taskRank } from '../palette';
import type { ReplayStore } from '../replay/store';

export const SHORT: Record<string, string> = {
  T1_ingest_wrist: 'Wrist cam', T1_ingest_overhead: 'Overhead cam', T2_detect: 'Detect', T2_detect_overhead: 'Detect (overhead)', T10_record_4k: 'Record 4K', T3_esdf: 'ESDF', T4_vla: 'VLA',
  T5_plan: 'Plan', T6_estimate: 'Estimate', T7_control: 'Control', T8_safety: 'Safety', T10_record: 'Record',
  T11_speech: 'Speech', T12_arm2_control: 'Arm 2 control', T13_map_save: 'Map save', T14_ota: 'OTA', T15_diag: 'Diagnostics',
};

const R = 21;
const C = 2 * Math.PI * R;

interface Ring {
  task: number;
  el: HTMLElement;
  arc: SVGCircleElement;
  track: SVGCircleElement;
  value: HTMLElement;
  lastMiss: number;
  flashUntil: number;
  x: number; y: number;
}

export class DeadlineRings {
  private rings: Ring[] = [];
  private store: ReplayStore | null = null;
  onSelect: ((task: number) => void) | null = null;
  /** Horizontal room a rail needs (px), read by the die fit. */
  margin = 130;
  compact = false;
  constructor(private host: HTMLElement) {}

  setStore(store: ReplayStore): void {
    this.store = store;
    this.host.innerHTML = '';
    this.rings = [];
    const order = store.manifest.tasks.map((t, i) => [t, i] as const).sort((a, b) => taskRank(a[0].id) - taskRank(b[0].id));
    order.forEach(([t, i]) => {
      if (!t.deadline_us) return;
      const el = document.createElement('button');
      el.className = 'ring';
      el.title = `${t.name}: deadline ${(t.deadline_us / 1000).toLocaleString('en-US')} ms. Click to jump to the latest job.`;
      const color = taskColor(t.id, t.color);
      el.style.setProperty('--task', color);
      el.innerHTML = `
        <svg viewBox="0 0 54 54" width="54" height="54" aria-hidden="true">
          <circle class="ring-track" cx="27" cy="27" r="${R}" />
          <circle class="ring-arc" cx="27" cy="27" r="${R}" stroke-dasharray="${C}" stroke-dashoffset="${C}" />
        </svg>
        <div class="ring-text"><div class="ring-name"></div><div class="ring-value"></div></div>`;
      (el.querySelector('.ring-name') as HTMLElement).textContent = SHORT[t.id] ?? t.name;
      el.addEventListener('click', () => this.onSelect?.(i));
      this.host.appendChild(el);
      this.rings.push({
        task: i, el, arc: el.querySelector('.ring-arc') as SVGCircleElement, track: el.querySelector('.ring-track') as SVGCircleElement,
        value: el.querySelector('.ring-value') as HTMLElement, lastMiss: -1, flashUntil: 0, x: 0, y: 0,
      });
    });
  }

  layout(ell: { cx: number; cy: number; rx: number; ry: number }, w: number, h: number, leftInset: number, rightInset: number): void {
    const st = this.store;
    const vis = this.rings.filter((r) => (st ? (st.jobsByTask[r.task]?.length ?? 0) > 0 : true));
    for (const r of this.rings) r.el.style.display = vis.includes(r) ? '' : 'none';
    const n = vis.length;
    if (!n) return;
    // two rails flanking the die, following its projected edges
    const left = Math.ceil(n / 2);
    const top = 128, bottom = h - 100; // keep clear of the voltage legend in the bottom-left corner
    const step = (bottom - top) / Math.max(left, n - left);
    // many tasks: switch to compact rows (smaller ring, label beside it) instead of shrinking text
    // with the drawer open, rings shrink to dots-with-arcs (details stay in each ring's tooltip)
    const mini = rightInset > 0;
    const compact = mini || step < 96;
    this.compact = compact;
    this.margin = mini ? 62 : compact ? 178 : 130;
    const half = mini ? 16 : compact ? 20 : 27;
    const lx = compact ? leftInset + 16 + half : leftInset > 0 ? 62 : Math.max(56, Math.min(ell.cx - ell.rx - 70, w * 0.5 - 120));
    const rx = compact ? w - rightInset - 16 - half : Math.min(w - rightInset - 62, Math.max(ell.cx + ell.rx + 70, w * 0.5 + 120));
    vis.forEach((r, i) => {
      const col = i < left ? 0 : 1;
      const k = col === 0 ? i : i - left;
      const cnt = col === 0 ? left : n - left;
      const x = col === 0 ? lx : rx;
      const y = top + ((bottom - top) * (k + 0.5)) / cnt;
      r.x = x; r.y = y;
      r.el.classList.toggle('left-rail', col === 0);
      r.el.classList.toggle('compact', compact);
      r.el.classList.toggle('mini', mini);
      r.el.classList.toggle('text-left', compact && col === 1);
      r.el.style.transform = `translate(${(x - half).toFixed(1)}px, ${(y - half).toFixed(1)}px)`;
    });
  }

  update(t: number, speed: number, nowMs: number): void {
    const st = this.store;
    if (!st) return;
    const tasks = st.manifest.tasks;
    for (const r of this.rings) {
      const task = tasks[r.task];
      const arr = st.jobsByTask[r.task];
      const latest = st.latestJob(r.task, t);
      let fill = 0;
      let state: 'idle' | 'run' | 'ok' | 'miss' = 'idle';
      let txt = 'idle';
      const periodPs = (task.period_us ?? 0) * 1e6;
      const wallPeriod = periodPs / 1e12 / Math.max(1e-9, speed);
      if (latest >= 0) {
        const j = st.jobView(latest);
        const rel = j.deadline - j.release;
        const winPs = Math.max(periodPs * 2, speed * 0.4e12);
        const recent = periodPs > 0 && wallPeriod < 0.09 ? st.firstJobPos(r.task, t + 1) - st.firstJobPos(r.task, t - winPs) : 0;
        if (recent > 0) {
          // peak hold over the last 0.4 s of wall time, so 1 kHz loops stay readable
          const win = Math.max(periodPs * 2, speed * 0.4e12);
          const from = st.firstJobPos(r.task, t - win);
          let worst = 0, missed = false, cnt = 0, worstResp = 0;
          for (let p = from; p < arr.length; p++) {
            const ji = arr[p];
            if (st.jobs.release[ji] > t) break;
            cnt++;
            const fin = st.jobs.finish[ji];
            const done = fin >= 0 && fin <= t;
            const resp = (done ? fin : t) - st.jobs.release[ji];
            const ratio = resp / Math.max(1, st.jobs.deadline[ji] - st.jobs.release[ji]);
            if (ratio > worst) { worst = ratio; worstResp = resp; }
            if ((st.jobs.flags[ji] & 1) && st.jobs.deadline[ji] <= t) missed = true;
          }
          fill = Math.min(1, worst);
          state = missed ? 'miss' : 'ok';
          txt = `${fmtMs(worstResp)} worst of ${cnt}`;
        } else if (t < j.finish || j.finish < 0) {
          fill = Math.min(1, (t - j.release) / Math.max(1, rel));
          state = t > j.deadline ? 'miss' : 'run';
          txt = `${fmtMs(t - j.release)} / ${fmtMs(rel)}`;
        } else {
          fill = Math.min(1, (j.finish - j.release) / Math.max(1, rel));
          state = j.missed ? 'miss' : 'ok';
          txt = `${fmtMs(j.finish - j.release)} / ${fmtMs(rel)}`;
        }
        if (j.missed && j.deadline <= t && r.lastMiss !== j.index) {
          r.lastMiss = j.index;
          r.flashUntil = nowMs + 900;
        }
      }
      const flashing = nowMs < r.flashUntil;
      r.arc.style.strokeDashoffset = String(C * (1 - fill));
      r.el.dataset.state = flashing ? 'flash' : state;
      r.arc.style.stroke = state === 'miss' || flashing ? MISS_RED : '';
      if (r.value.textContent !== txt) {
        r.value.textContent = txt;
        if (r.el.classList.contains('mini')) r.el.title = `${SHORT[task.id] ?? task.name}: ${txt}. Click to jump to the latest job.`;
      }
    }
  }
}

export function fmtMs(ps: number): string {
  const ms = ps / 1e9;
  if (ms >= 100) return `${ms.toFixed(0)} ms`;
  if (ms >= 10) return `${ms.toFixed(1)} ms`;
  if (ms >= 1) return `${ms.toFixed(2)} ms`;
  return `${(ms * 1000).toFixed(0)} µs`;
}
