// Selection card and the "Why did this miss?" breakdown.
import { MISS_RED, taskColor } from '../palette';
import type { ReplayStore } from '../replay/store';
import type { CritOp } from '../replay/types';
import { blockLabel } from '../views/die';
import { SHORT, fmtMs } from '../views/rings';

const ATTR_LABEL: Record<string, string> = {
  compute: 'Compute', runqueue: 'CPU run queue', engine_queue: 'Engine queue', noc: 'NoC', dram: 'DRAM', cache: 'Cache',
  dma: 'DMA', preemption: 'Preemption', dvfs: 'DVFS transition', middleware: 'Middleware',
};
const ATTR_COLOR: Record<string, string> = {
  compute: '#7D8BFF', runqueue: '#FFD166', engine_queue: '#FF9F45', noc: '#5EC8F2', dram: '#FF4D5E', cache: '#2ED3B0',
  dma: '#A9BCD0', preemption: '#FF8BB4', dvfs: '#B47CFF', middleware: '#86DBA6',
};

export function engineLabel(e: string): string {
  const m = /^([a-z_]+?)(\d+)$/.exec(e);
  if (!m) return blockLabel(e);
  const kind = m[1] === 'npu' ? 'tile' : m[1] === 'simt' ? 'CU' : m[1] === 'dma' ? 'engine' : 'core';
  return `${blockLabel(m[1])} ${kind} ${m[2]}`;
}

export class SelectionCard {
  onWhy: ((jobIndex: number) => void) | null = null;
  onClose: (() => void) | null = null;
  constructor(private host: HTMLElement) {}

  hide(): void {
    this.host.classList.remove('open');
  }

  show(st: ReplayStore, ji: number, crit: CritOp[] | null, why: boolean): void {
    const j = st.jobView(ji);
    const task = st.manifest.tasks[j.task];
    const color = taskColor(task.id, task.color);
    const resp = j.finish - j.release;
    const rel = j.deadline - j.release;
    const attr = st.attribution(ji);
    const keys = Object.keys(attr).filter((k) => attr[k] > 0).sort((a, b) => attr[b] - attr[a]);
    const total = keys.reduce((s, k) => s + attr[k], 0) || 1;
    const over = resp - rel;
    const whyHtml = why ? `
      <div class="why">
        <div class="why-title">Where the ${fmtMs(resp)} went</div>
        <div class="stack">${keys.map((k) => `<span style="width:${((attr[k] / total) * 100).toFixed(2)}%;background:${ATTR_COLOR[k] ?? '#ccc'}" title="${ATTR_LABEL[k] ?? k}"></span>`).join('')}</div>
        <div class="attr-list">${keys.slice(0, 6).map((k) => `
          <div class="attr-row"><span class="dot" style="background:${ATTR_COLOR[k] ?? '#ccc'}"></span><span>${ATTR_LABEL[k] ?? k}</span>
          <span class="kv-num">${fmtMs(attr[k] * 1000)}</span><span class="kv-num pct">${((attr[k] / total) * 100).toFixed(0)}%</span></div>`).join('')}</div>
        ${crit ? `<div class="why-title">Critical path, ${crit.length} ops</div>
        <div class="crit-list">${crit.filter((c) => c.wait_ps > 0 || c.wait_cause !== 'none').slice(0, 5).map((c) => `
          <div class="crit-row"><span>${c.op_id}</span><span>${engineLabel(c.engine)}</span><span class="kv-num">${c.wait_cause === 'none' ? '' : `${ATTR_LABEL[c.wait_cause] ?? c.wait_cause}${c.wait_ps > 0 ? ' wait ' + fmtMs(c.wait_ps) : ''}`}</span></div>`).join('') || '<div class="crit-row"><span>No queueing waits recorded on the path</span></div>'}</div>` : ''}
      </div>` : '';
    this.host.innerHTML = `
      <div class="sel-head">
        <span class="dot big" style="background:${color}"></span>
        <div><div class="sel-title">${SHORT[task.id] ?? task.name} <span class="sel-sub">job ${j.job}</span></div>
          <div class="sel-meta">${j.missed ? `<b style="color:${MISS_RED}">Missed by ${fmtMs(over)}</b>` : `Met with ${fmtMs(-over)} slack`}</div></div>
        <button class="icon-btn" data-close aria-label="Close">✕</button>
      </div>
      <div class="sel-grid">
        <div><div class="k">Response</div><div class="v">${fmtMs(resp)}</div></div>
        <div><div class="k">Deadline</div><div class="v">${fmtMs(rel)}</div></div>
        <div><div class="k">Queued</div><div class="v">${fmtMs(Math.max(0, j.start - j.release))}</div></div>
      </div>
      ${j.missed && !why ? `<button class="btn primary wide" data-why>Why did this miss?</button>` : ''}
      ${whyHtml}`;
    this.host.classList.add('open');
    this.host.querySelector('[data-close]')?.addEventListener('click', () => this.onClose?.());
    this.host.querySelector('[data-why]')?.addEventListener('click', () => this.onWhy?.(ji));
  }
}

export function dominantCause(st: ReplayStore, ji: number): string {
  const a = st.attribution(ji);
  let best = 'compute', v = -1;
  for (const [k, x] of Object.entries(a)) if (k !== 'compute' && x > v) { v = x; best = k; }
  return best;
}
