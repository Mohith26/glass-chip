// Engineer mode: Perfetto/Chrome trace export, results JSON export, YAML config viewer.
import type { ReplayStore } from '../replay/store';
import { KIND, NONE16 } from '../replay/types';
import { SHORT } from '../views/rings';

function download(name: string, data: string, type = 'application/json'): void {
  const url = URL.createObjectURL(new Blob([data], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Chrome JSON trace (opens in ui.perfetto.dev) for events in [t0, t1]. Times in microseconds. */
export async function exportPerfetto(st: ReplayStore, t0: number, t1: number): Promise<number> {
  await st.ensureEvents(t0, t1);
  const m = st.manifest;
  const ev: Record<string, unknown>[] = [];
  const blocks = m.soc.blocks;
  blocks.forEach((b, i) => ev.push({ ph: 'M', name: 'process_name', pid: i + 1, args: { name: b.id } }));
  const nocPid = blocks.length + 1;
  const taskPid = blocks.length + 2;
  ev.push({ ph: 'M', name: 'process_name', pid: nocPid, args: { name: 'NoC transfers' } });
  ev.push({ ph: 'M', name: 'process_name', pid: taskPid, args: { name: 'Robot tasks (jobs)' } });
  const us = (ps: number): number => ps / 1e6;
  const strings = m.events.strings;
  for (const b of st.loadedBlocks(t0, t1)) {
    for (let i = 0; i < b.n; i++) {
      const s = b.t[i], e = s + b.dur[i];
      if (e < t0 || s > t1) continue;
      const task = m.tasks[b.task[i]]?.id ?? '';
      const name = b.op[i] < strings.length ? strings[b.op[i]] : task;
      if (b.kind[i] === KIND.op) {
        ev.push({ ph: 'X', name, cat: task, pid: b.block[i] + 1, tid: b.unit[i], ts: us(s), dur: us(b.dur[i]),
          args: { task, job: b.job[i], layer: b.aux[i], critical: (b.flags[i] & 1) === 1 } });
      } else if (b.kind[i] === KIND.xfer && b.src[i] !== NONE16) {
        ev.push({ ph: 'X', name: `${name} xfer`, cat: task, pid: nocPid, tid: b.src[i] * 100 + b.dst[i], ts: us(s), dur: us(b.dur[i]),
          args: { src: b.src[i], dst: b.dst[i], bytes: b.bytes[i], task, job: b.job[i] } });
      }
    }
  }
  m.tasks.forEach((t, ti) => {
    ev.push({ ph: 'M', name: 'thread_name', pid: taskPid, tid: ti, args: { name: SHORT[t.id] ?? t.id } });
    const arr = st.jobsByTask[ti];
    for (let p = st.firstJobPos(ti, t0 - 5e11); p < arr.length; p++) {
      const ji = arr[p];
      const r = st.jobs.release[ji];
      if (r > t1) break;
      const f = st.jobs.finish[ji];
      if (f < t0) continue;
      ev.push({ ph: 'X', name: `${t.id} job ${st.jobs.job[ji]}`, cat: 'job', pid: taskPid, tid: ti, ts: us(r), dur: us(Math.max(0, f - r)),
        args: { deadline_us: us(st.jobs.deadline[ji]), missed: (st.jobs.flags[ji] & 1) === 1, ...st.attribution(ji) } });
    }
  });
  const meta = { replay: m.name, synthetic: m.synthetic, scenario: m.scenario, policy: m.policy, seed: m.seed, window_ps: [t0, t1], provenance: m.provenance };
  download(`glasschip_${m.scenario}_${m.policy}_${Math.round(us(t0))}us.json`, JSON.stringify({ traceEvents: ev, displayTimeUnit: 'ns', metadata: meta }));
  return ev.length;
}

export function exportResults(st: ReplayStore): void {
  const m = st.manifest;
  download(`glasschip_results_${m.scenario}_${m.policy}.json`, JSON.stringify({
    replay: m.name, scenario: m.scenario, policy: m.policy, seed: m.seed, synthetic: m.synthetic,
    provenance: m.provenance, results: m.results,
  }, null, 2));
}

function highlightYaml(src: string): string {
  const esc = (s: string): string => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return src.split('\n').map((line) => {
    const ci = line.indexOf('#');
    const code = ci >= 0 ? line.slice(0, ci) : line;
    const comment = ci >= 0 ? line.slice(ci) : '';
    let h = esc(code).replace(/^(\s*-?\s*)([A-Za-z0-9_]+)(:)/, '$1<span class="y-key">$2</span>$3')
      .replace(/\b(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b/gi, '<span class="y-num">$1</span>');
    if (comment) h += `<span class="y-com">${esc(comment)}</span>`;
    return h;
  }).join('\n');
}

export class EngineerPanel {
  private open = false;
  constructor(private host: HTMLElement, private getStore: () => ReplayStore | null, private getWindow: () => [number, number]) {}

  toggle(force?: boolean): void {
    this.open = force ?? !this.open;
    this.host.classList.toggle('open', this.open);
    if (this.open) void this.render();
  }

  private async render(): Promise<void> {
    const st = this.getStore();
    if (!st) return;
    const m = st.manifest;
    let files: string[];
    try {
      files = ((await (await fetch('configs/index.json')).json()) as { files: string[] }).files;
      files.sort((a, b) => (a.startsWith('soc/') ? 0 : 1) - (b.startsWith('soc/') ? 0 : 1) || a.localeCompare(b));
    } catch { files = []; }
    this.host.innerHTML = `
      <div class="sheet-head"><h2>Engineer mode</h2><button class="icon-btn" data-close aria-label="Close">✕</button></div>
      <div class="sheet-body">
        <section class="card">
          <header><h3>Export</h3></header>
          <p class="note">The trace covers the timeline's visible window and opens in ui.perfetto.dev or chrome://tracing.</p>
          <div class="row-actions">
            <button class="btn" data-act="perfetto">Export Perfetto trace</button>
            <button class="btn" data-act="results">Export results JSON</button>
          </div>
          <div class="foot" data-status></div>
        </section>
        <section class="card">
          <header><h3>Replay</h3></header>
          <dl class="meta">
            <dt>Name</dt><dd>${m.name}</dd>
            <dt>Scenario</dt><dd>${m.scenario}</dd>
            <dt>Policy</dt><dd>${m.policy}</dd>
            <dt>Seed</dt><dd>${m.seed}</dd>
            <dt>Events</dt><dd>${m.events.count.toLocaleString('en-US')}</dd>
            <dt>Jobs</dt><dd>${m.jobs.count.toLocaleString('en-US')}</dd>
            <dt>Producer</dt><dd>${String(m.provenance.script ?? 'unknown')}</dd>
            <dt>Data</dt><dd>${m.synthetic ? '<b class="warn">Synthetic dev data</b>' : 'Simulator output'}</dd>
          </dl>
        </section>
        <section class="card">
          <header><h3>Config</h3>
            <select data-file>${files.map((f) => `<option>${f}</option>`).join('')}</select></header>
          <pre class="yaml" data-yaml>${files.length ? 'Loading…' : 'No configs synced. Run npm run dev or npm run build.'}</pre>
        </section>
      </div>`;
    this.host.querySelector('[data-close]')?.addEventListener('click', () => this.toggle(false));
    const status = this.host.querySelector('[data-status]') as HTMLElement;
    this.host.querySelector('[data-act="perfetto"]')?.addEventListener('click', async () => {
      const [a, b] = this.getWindow();
      status.textContent = 'Building trace…';
      const n = await exportPerfetto(st, a, b);
      status.textContent = `Exported ${n.toLocaleString('en-US')} trace events.`;
    });
    this.host.querySelector('[data-act="results"]')?.addEventListener('click', () => exportResults(st));
    const sel = this.host.querySelector('[data-file]') as HTMLSelectElement | null;
    const yaml = this.host.querySelector('[data-yaml]') as HTMLElement;
    const load = async (): Promise<void> => {
      if (!sel || !sel.value) return;
      const txt = await (await fetch(`configs/${sel.value}`)).text();
      yaml.innerHTML = highlightYaml(txt);
    };
    sel?.addEventListener('change', () => void load());
    void load();
  }
}
