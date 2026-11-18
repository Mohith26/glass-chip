// Guided tour (about 3 minutes), following the demo story in spec section 13.5.
// Captions are built from the replays that actually exist: every number comes from a manifest,
// and a step that the data cannot show says so instead of narrating it.
import type { Manifest, ReplayIndexEntry } from '../replay/types';
import { SHORT } from '../views/rings';
import { nowMs } from '../util/time';

export interface TourHost {
  replays(): ReplayIndexEntry[];
  hasReplay(id: string): boolean;
  loadReplay(id: string, keepTime: boolean): Promise<void>;
  manifestOf(id: string): Promise<Manifest | null>;
  seekSeconds(s: number): void;
  setSpeed(s: number): void;
  setPlaying(p: boolean): void;
  selectMiss(taskId: string, afterS: number, why: boolean): Promise<boolean>;
  clearSelection(): void;
  openDrawer(open: boolean, section?: 'dse' | 'power' | 'top'): void;
  highlight(selector: string | null): void;
  replayId(): string;
  surrogateLoaded(): boolean;
  /** Release time (s) of the first missed job in the loaded replay, or null. */
  firstMissSeconds(): Promise<number | null>;
}

interface Step {
  at: number; // seconds into the tour
  title: string;
  run: (h: TourHost) => Promise<string>;
}

type PS = { energy_per_robot_hour_wh?: number; avg_power_w?: number };
type Q = { p50?: number | null; p99?: number | null; mean?: number | null; max?: number | null };
type RM = {
  oscillation_episodes?: number; objects_missed?: number; placed?: number; objects_missed_per_min?: number | null;
  place_success_rate?: number | null; min_human_separation_m?: number | null; min_human_separation_moving_m?: number | null;
  safety_stop_latency_ms?: Q;
  chain_latency_ms?: Record<string, Q>;
};
const rmOf = (m: Manifest | null): RM | undefined => (m as unknown as { robot_metrics?: RM } | null)?.robot_metrics;

const fmt = (v: number | null | undefined, d = 1): string => (v == null || !Number.isFinite(v) ? 'n/a' : v.toFixed(d));

function misses(m: Manifest): { total: number; byTask: [string, number][] } {
  const t = m.results?.tasks ?? {};
  const by = Object.entries(t).map(([k, v]) => [k, v.misses] as [string, number]).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  return { total: by.reduce((s, [, n]) => s + n, 0), byTask: by };
}

function p99(m: Manifest, task: string): number | null {
  return m.results?.tasks?.[task]?.p99_ms ?? null;
}

function deadlineMs(m: Manifest, task: string): number | null {
  const t = m.tasks.find((x) => x.id === task);
  return t?.deadline_us ? t.deadline_us / 1000 : null;
}

function powerSummary(m: Manifest): PS {
  const ps = (m as unknown as { power_summary?: PS }).power_summary;
  return ps ?? { energy_per_robot_hour_wh: m.results?.energy_per_robot_hour_wh ?? undefined, avg_power_w: m.results?.avg_power_w };
}

function real(h: TourHost): ReplayIndexEntry[] {
  const r = h.replays();
  const nonSyn = r.filter((x) => !x.synthetic);
  return nonSyn.length ? nonSyn : r;
}

function find(h: TourHost, scen: string, pol: string): ReplayIndexEntry | undefined {
  return real(h).find((r) => r.scenario.startsWith(scen) && r.policy.startsWith(pol));
}

const STEPS: Step[] = [
  {
    at: 0, title: 'The loop',
    run: async (h) => {
      const e = find(h, 'S1', 'P0') ?? real(h)[0];
      if (e) await h.loadReplay(e.id, false);
      h.seekSeconds(1.5); h.setSpeed(1); h.setPlaying(true); h.openDrawer(false); h.clearSelection(); h.highlight(null);
      const m = e ? await h.manifestOf(e.id) : null;
      const src = (m as unknown as { behavior_source?: string } | null)?.behavior_source;
      return 'On the left, a simulated robot arm picks parts off a moving conveyor. On the right, VX-1, a fictional robot SoC, runs every task that arm needs in simulated time.' +
        (src ? ` The arm is driven by a ${src.split(';')[0]}.` : '');
    },
  },
  {
    at: 20, title: 'Deadlines',
    run: async (h) => {
      h.highlight('#rings');
      const m = await h.manifestOf(h.replayId());
      if (!m) return 'Each ring is a real-time deadline that fills while its job runs and turns red if the job misses.';
      const det = deadlineMs(m, 'T2_detect'), ctl = deadlineMs(m, 'T7_control');
      return `Each ring is a real-time deadline. In this run detection gets ${fmt(det, 0)} ms and joint control gets ${fmt(ctl, 0)} ms. A ring fills while its job runs and turns red if the job misses.`;
    },
  },
  {
    at: 32, title: 'Add a second camera',
    run: async (h) => {
      h.highlight('#scenarios');
      const e2 = find(h, 'S2', 'P0');
      if (!e2) return 'There is no second-camera replay yet, so this step is skipped.';
      const e1 = find(h, 'S1', 'P0');
      const m1 = e1 ? await h.manifestOf(e1.id) : null;
      const m2 = await h.manifestOf(e2.id);
      await h.loadReplay(e2.id, false);
      const ev = m2?.scenario_events?.[0];
      h.seekSeconds(Math.max(0, (ev?.t_s ?? 4) - 0.8)); h.setSpeed(0.5); h.setPlaying(true);
      if (!m2) return '';
      const mm = misses(m2);
      const v1 = m1 ? p99(m1, 'T4_vla') : null, v2 = p99(m2, 'T4_vla');
      return `At ${fmt(ev?.t_s ?? null, 0)} s the overhead camera starts feeding a second detector. VLA p99 response ${m1 ? `goes from ${fmt(v1)} ms to ` : 'is '}${fmt(v2)} ms against its ${fmt(deadlineMs(m2, 'T4_vla'), 0)} ms deadline` +
        (mm.total ? `, and ${mm.total} jobs miss their deadlines.` : ', and every deadline still holds in this run.');
    },
  },
  {
    at: 46, title: 'Why did this miss?',
    run: async (h) => {
      h.highlight(null);
      // preferred story: a safety-monitor miss while a person walks into the cell under heavy recording
      const s9 = find(h, 'S9', 'P0');
      const m9 = s9 ? await h.manifestOf(s9.id) : null;
      if (s9 && m9 && (m9.results?.tasks?.T8_safety?.misses ?? 0) > 0) {
        await h.loadReplay(s9.id, false);
        h.setPlaying(false);
        const entry = m9.robot?.events.find((e) => e.kind === 'protective_zone_entry' || e.kind === 'safety_stop');
        const ok = await h.selectMiss('T8_safety', entry ? Math.max(0, entry.t_ps / 1e12 - 0.3) : 0, true);
        if (ok) return `"${s9.scenario_label}": the safety monitor misses its ${fmt(deadlineMs(m9, 'T8_safety'), 0)} ms deadline while a person walks in. The card shows where this job's time went and the die animates its critical path.`;
      }
      let best: { e: ReplayIndexEntry; m: Manifest; n: number } | null = null;
      for (const e of real(h)) {
        const m = await h.manifestOf(e.id);
        if (!m) continue;
        const n = misses(m).total;
        if (n > 0 && (!best || n > best.n)) best = { e, m, n };
      }
      if (!best) return 'None of the recorded replays has a missed deadline, so there is no miss to explain here.';
      await h.loadReplay(best.e.id, false);
      h.setPlaying(false);
      // explain a miss of the most critical task that missed (a late best-effort recording job tells less)
      const rank: Record<string, number> = { safety: 0, high: 1, medium: 2, low: 3 };
      const crit = (id: string): number => rank[best!.m.tasks.find((x) => x.id === id)?.criticality ?? 'low'] ?? 3;
      const [task, n] = [...misses(best.m).byTask].sort((a, b) => crit(a[0]) - crit(b[0]) || b[1] - a[1])[0];
      const ok = await h.selectMiss(task, 0, true);
      return ok ? `In "${best.e.scenario_label} / ${best.e.policy_label}", ${SHORT[task] ?? task} misses ${n} times. The card shows where this job's time went and the die animates its critical path.`
        : 'Could not select the missed job.';
    },
  },
  {
    at: 64, title: 'Arbitration and partitioning',
    run: async (h) => {
      h.clearSelection();
      for (const sc of ['S7', 'S2']) {
        const ea = find(h, sc, 'P0');
        if (!ea) continue;
        const ma = await h.manifestOf(ea.id);
        if (!ma) continue;
        // among the other policies for this scenario, show the one that removes the most misses
        let best: { e: ReplayIndexEntry; m: Manifest } | null = null;
        for (const e of real(h).filter((r) => r.scenario.startsWith(sc) && !r.policy.startsWith('P0') && !r.policy.startsWith('P4'))) {
          const m = await h.manifestOf(e.id);
          if (m && (!best || misses(m).total < misses(best.m).total)) best = { e, m };
        }
        if (!best || misses(ma).total === 0) continue;
        await h.loadReplay(best.e.id, false);
        h.seekSeconds(4); h.setSpeed(0.5); h.setPlaying(true); h.highlight('#policies');
        const crit = (m: Manifest): number => misses(m).byTask
          .filter(([id]) => ['safety', 'high'].includes(m.tasks.find((x) => x.id === id)?.criticality ?? 'low'))
          .reduce((s2, [, n]) => s2 + n, 0);
        return `Same scenario and seed, scheduled with "${best.e.policy_label}" instead of "${ea.policy_label}": ${misses(ma).total} missed jobs become ${misses(best.m).total}, ` +
          `and misses on safety and high-criticality tasks go from ${crit(ma)} to ${crit(best.m)}.`;
      }
      return 'There is no matched pair of replays for this comparison yet, so this step is skipped.';
    },
  },
  {
    at: 80, title: 'Safety under load',
    run: async (h) => {
      h.highlight(null);
      const e0 = find(h, 'S9', 'P0');
      if (!e0) return 'The person-enters-during-recording replay (S9) has not been generated yet, so this step is skipped.';
      const others = real(h).filter((r) => r.scenario.startsWith('S9') && !r.policy.startsWith('P0'));
      const m0 = await h.manifestOf(e0.id);
      const e1 = others[0];
      const m1 = e1 ? await h.manifestOf(e1.id) : null;
      if (!m0) return '';
      const miss = (m: Manifest): string => fmt((m.results?.tasks?.T8_safety?.miss_rate ?? NaN) * 100, 0);
      const r0 = rmOf(m0), r1 = rmOf(m1);
      const hs = (r?: RM): string => fmt(r?.chain_latency_ms?.human_to_slowdown?.p99 ?? null, 0);
      const st = (r?: RM): string => fmt(r?.safety_stop_latency_ms?.mean ?? null, 0);
      // closest approach while the arm is still moving; the whole-run minimum includes a person leaning on a stopped arm
      const sep = (r?: RM): string => fmt(r?.min_human_separation_moving_m != null ? r.min_human_separation_moving_m * 100 : null, 0);
      await h.loadReplay(e0.id, false);
      const entry = m0.robot?.events.find((e) => e.kind === 'protective_zone_entry');
      h.seekSeconds(entry ? Math.max(0, entry.t_ps / 1e12 - 1) : 5); h.setSpeed(0.5); h.setPlaying(true);
      return `A person walks in while four 4K streams are recorded. With "${e0.policy_label}" the safety monitor misses ${miss(m0)}% of its deadlines and human-to-slowdown p99 is ${hs(r0)} ms` +
        (m1 && e1 ? `; with "${e1.policy_label}" it misses ${miss(m1)}% and p99 is ${hs(r1)} ms. Protective stops average ${st(r0)} ms against ${st(r1)} ms.` : '.') +
        (r0?.min_human_separation_moving_m != null
          ? ` The arm had already slowed, so the late monitor shows up as a thinner margin, not contact: the closest approach while the arm moves is ${sep(r0)} cm${m1 ? ` against ${sep(r1)} cm` : ''}.`
          : ' The arm had already slowed, so the consequence here is small.');
    },
  },
  {
    at: 98, title: 'Heat',
    run: async (h) => {
      const e = find(h, 'S5', 'P0');
      if (!e) return 'The hot-day replay (S5) has not been generated yet, so this step is skipped.';
      const m = await h.manifestOf(e.id);
      await h.loadReplay(e.id, false);
      h.seekSeconds(8); h.setSpeed(1); h.setPlaying(true); h.openDrawer(true, 'power');
      if (!m) return '';
      const r = rmOf(m);
      const ps = (m as unknown as { power_summary?: {
        time_throttled_s?: number; peak_temp_c?: number; ambient_c_end?: number;
        settle?: { runaway?: boolean; capped_at_c?: number } } }).power_summary;
      const spd = (m as unknown as { conveyor_speed_mps?: number }).conveyor_speed_mps;
      const amb = ps?.ambient_c_end != null ? `At ${fmt(ps.ambient_c_end, 0)} °C ambient` : 'On a hot day';
      // runaway settle: no steady state at the run's own power, so the run starts at the throttle threshold
      const start = ps?.settle?.runaway && ps.settle.capped_at_c != null
        ? `${amb} this module has no thermal steady state, so the run starts at the ${fmt(ps.settle.capped_at_c, 0)} °C throttle threshold`
        : `${amb} the module heats up`;
      return `${start}${ps?.peak_temp_c != null ? ` and peaks at ${fmt(ps.peak_temp_c, 0)} °C` : ''}` +
        (ps?.time_throttled_s != null ? `. It throttles for ${fmt(ps.time_throttled_s, 1)} s of the run` : '') +
        `. VLA p99 is ${fmt(p99(m, 'T4_vla'))} ms${spd ? ` with the belt at ${spd} m/s` : ''}, and ${fmt(r?.objects_missed_per_min ?? null, 1)} objects per minute reach the end of the belt unpicked. ` +
        'No scheduling policy fixes this within a run: the slowdown comes from the thermal state.';
    },
  },
  {
    at: 114, title: 'No points for finishing early',
    run: async (h) => {
      for (const s of ['S2', 'S1']) {
        const e0 = find(h, s, 'P0'), e4 = find(h, s, 'P4');
        if (!e0 || !e4) continue;
        const m0 = await h.manifestOf(e0.id), m4 = await h.manifestOf(e4.id);
        if (!m0 || !m4) continue;
        await h.loadReplay(e4.id, false);
        h.seekSeconds(6); h.setSpeed(1); h.setPlaying(true); h.openDrawer(true, 'power'); h.highlight('#drawer');
        const w0 = powerSummary(m0).energy_per_robot_hour_wh, w4 = powerSummary(m4).energy_per_robot_hour_wh;
        const pct = w0 && w4 ? (1 - w4 / w0) * 100 : null;
        return `Pace to deadline picks the lowest voltage that still meets each deadline. Domains cool toward blue; energy per robot-hour goes from ${fmt(w0, 2)} to ${fmt(w4, 2)} Wh` +
          `${pct != null ? ` (${fmt(Math.abs(pct), 0)}% ${pct >= 0 ? 'lower' : 'higher'})` : ''}, with ${misses(m4).total} missed jobs against ${misses(m0).total} at nominal.`;
      }
      return 'There is no pace-to-deadline replay yet, so this step is skipped.';
    },
  },
  {
    at: 132, title: 'The voltage cliff',
    run: async (h) => {
      h.highlight(null); h.openDrawer(false);
      const e = find(h, 'S8', 'P0');
      if (!e) return 'The low-voltage sweep replay (S8) has not been generated yet, so this step is skipped.';
      const m = await h.manifestOf(e.id);
      await h.loadReplay(e.id, false);
      // start just before the first voltage step that produces a miss, if there is one
      const firstMiss = await h.firstMissSeconds();
      const steps = (m?.scenario_events ?? []).map((x) => x.t_s).filter((t) => firstMiss == null || t <= firstMiss);
      h.seekSeconds(Math.max(0, (steps.length ? steps[steps.length - 1] : firstMiss ?? 2) - 1.5)); h.setSpeed(1); h.setPlaying(true);
      if (!m) return '';
      const mm = misses(m);
      const rm = (m as unknown as { robot_metrics?: RM }).robot_metrics;
      return `Here the NPU and CPU voltages step down from nominal toward their minimums.${firstMiss != null ? ` The first deadline miss comes at ${fmt(firstMiss, 1)} s.` : ''} This run records ${mm.total} missed jobs` +
        (mm.byTask.length ? ` (mostly ${SHORT[mm.byTask[0][0]] ?? mm.byTask[0][0]})` : '') +
        (rm?.oscillation_episodes != null ? ` and ${rm.oscillation_episodes} arm oscillation episodes.` : '.');
    },
  },
  {
    at: 150, title: 'Design space',
    run: async (h) => {
      h.openDrawer(true, 'dse'); h.highlight('#dse');
      return h.surrogateLoaded()
        ? 'The design-space panel runs a learned surrogate in the browser. Each output shows its conformal interval, and wide intervals are flagged so you know to run the full model.'
        : 'The design-space panel will run a learned surrogate with conformal error bars in the browser. The model is not published yet, so the panel stays disabled.';
    },
  },
  {
    at: 166, title: 'Open it up',
    run: async (h) => {
      h.highlight('#btn-engineer'); h.openDrawer(false);
      return 'Engineer mode exports the visible window as a Perfetto trace, exports results JSON, and shows the exact configs behind this run.';
    },
  },
];
const TOUR_LEN = 180;

export class Tour {
  active = false;
  private t0 = 0;
  private idx = -1;
  private paused = false;
  private pauseAt = 0;
  private text = '';
  /** The running step's async work (loads, selections); capture mode awaits it before drawing. */
  pending: Promise<unknown> = Promise.resolve();
  constructor(private host: HTMLElement, private h: TourHost) {}

  start(): void {
    this.active = true;
    this.idx = -1;
    this.t0 = nowMs();
    this.paused = false;
    this.host.classList.add('open');
  }

  stop(): void {
    this.active = false;
    this.host.classList.remove('open');
    this.h.highlight(null);
  }

  private elapsed(): number {
    return ((this.paused ? this.pauseAt : nowMs()) - this.t0) / 1000;
  }

  private go(i: number): void {
    const s = STEPS[i];
    this.idx = i;
    this.t0 = nowMs() - s.at * 1000;
    this.text = '';
    this.render();
    this.pending = s.run(this.h).then((txt) => { if (this.idx === i) { this.text = txt; this.render(); } });
  }

  tick(): void {
    if (!this.active || this.paused) return;
    const e = this.elapsed();
    let next = this.idx;
    while (next + 1 < STEPS.length && STEPS[next + 1].at <= e) next++;
    if (next !== this.idx) this.go(next);
    const bar = this.host.querySelector('.tour-progress-fill') as HTMLElement | null;
    if (bar) bar.style.width = `${Math.min(100, (e / TOUR_LEN) * 100).toFixed(2)}%`;
    if (e > TOUR_LEN) this.stop();
  }

  private render(): void {
    const s = STEPS[Math.max(0, this.idx)];
    this.host.innerHTML = `
      <div class="tour-card">
        <div class="tour-step">${Math.max(1, this.idx + 1)} of ${STEPS.length}</div>
        <div class="tour-title">${s.title}</div>
        <div class="tour-text">${this.text || '<span class="tour-wait">Loading this step…</span>'}</div>
        <div class="tour-progress"><div class="tour-progress-fill"></div></div>
        <div class="tour-actions">
          <button class="btn small" data-a="prev">Back</button>
          <button class="btn small" data-a="pause">${this.paused ? 'Resume' : 'Pause'}</button>
          <button class="btn small" data-a="next">Next</button>
          <button class="btn small ghost" data-a="exit">End tour</button>
        </div>
      </div>`;
    this.host.querySelector('[data-a="prev"]')?.addEventListener('click', () => this.go(Math.max(0, this.idx - 1)));
    this.host.querySelector('[data-a="next"]')?.addEventListener('click', () => (this.idx + 1 < STEPS.length ? this.go(this.idx + 1) : this.stop()));
    this.host.querySelector('[data-a="exit"]')?.addEventListener('click', () => this.stop());
    this.host.querySelector('[data-a="pause"]')?.addEventListener('click', () => {
      if (this.paused) { this.t0 += nowMs() - this.pauseAt; this.paused = false; }
      else { this.pauseAt = nowMs(); this.paused = true; }
      this.render();
    });
  }
}
