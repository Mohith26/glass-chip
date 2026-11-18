// Guided tour (about 3 minutes), following the demo script in spec section 13.5.

export interface TourHost {
  hasReplay(id: string): boolean;
  loadReplay(id: string, keepTime: boolean): Promise<void>;
  seekSeconds(s: number): void;
  setSpeed(s: number): void;
  setPlaying(p: boolean): void;
  selectMiss(taskId: string, afterS: number, why: boolean): Promise<boolean>;
  clearSelection(): void;
  openDrawer(open: boolean, section?: 'dse' | 'power' | 'top'): void;
  highlight(selector: string | null): void;
  replayId(): string;
}

interface Step {
  at: number; // seconds into the tour
  title: string;
  text: string;
  run: (h: TourHost) => Promise<string | void>;
}

const STEPS: Step[] = [
  { at: 0, title: 'The loop', text: 'On the left, a robot arm picks parts off a moving conveyor. On the right, VX-1, a fictional robot SoC, runs every task that arm needs in simulated time.',
    run: async (h) => { await h.loadReplay('S1_P0', false); h.seekSeconds(1.5); h.setSpeed(1); h.setPlaying(true); h.openDrawer(false); h.clearSelection(); h.highlight(null); } },
  { at: 25, title: 'Deadlines', text: 'Each ring is a real-time deadline. Perception gets 33 ms, control gets 1 ms. A ring fills while its job runs and turns red if the job misses.',
    run: async (h) => { h.highlight('#rings'); } },
  { at: 40, title: 'Add a second camera', text: 'At 6 s the overhead camera starts feeding detection. Detection now runs twice per frame, the DRAM queues grow, and the VLA ring starts missing.',
    run: async (h) => { h.highlight('#scenarios'); await h.loadReplay('S2_P0', false); h.seekSeconds(5.2); h.setSpeed(0.5); h.setPlaying(true); } },
  { at: 60, title: 'Why did this miss?', text: 'Pick a missed VLA job. Its critical path lights up through the memory controller, and the breakdown shows where the time went.',
    run: async (h) => { h.highlight(null); h.setPlaying(false); const ok = await h.selectMiss('T4_vla', 6, true); if (!ok) return 'No missed VLA job in this replay.'; } },
  { at: 80, title: 'QoS and cache partitioning', text: 'Same scenario, same seed, with priority arbitration for real-time traffic. The rings go green again.',
    run: async (h) => { h.clearSelection(); if (!h.hasReplay('S2_P2Q')) return 'That replay has not been generated yet.'; await h.loadReplay('S2_P2Q', true); h.seekSeconds(8); h.setSpeed(0.5); h.setPlaying(true); h.highlight('#policies'); } },
  { at: 100, title: 'No points for finishing early', text: 'Pace to deadline lowers each domain to the slowest voltage that still meets its deadlines. Domains cool toward blue and SoC power drops while the rings stay green.',
    run: async (h) => { if (!h.hasReplay('S2_P4Q')) return 'That replay has not been generated yet.'; await h.loadReplay('S2_P4Q', true); h.seekSeconds(8); h.setSpeed(1); h.setPlaying(true); h.openDrawer(true, 'power'); h.highlight('#drawer'); } },
  { at: 130, title: 'The voltage cliff', text: 'Lowering Vmin further eventually breaks the control loop: the control ring flickers and the arm oscillates.',
    run: async (h) => { h.highlight(null); h.openDrawer(false); if (!h.hasReplay('S8_P0')) return 'The low-voltage sweep replay (S8) has not been generated yet, so this step is skipped.'; await h.loadReplay('S8_P0', false); h.seekSeconds(2); h.setSpeed(0.5); h.setPlaying(true); } },
  { at: 150, title: 'Design space', text: 'The design-space panel runs a learned surrogate with conformal error bars in the browser, so sliders update instantly.',
    run: async (h) => { h.openDrawer(true, 'dse'); h.highlight('#dse'); } },
  { at: 170, title: 'Open it up', text: 'Engineer mode exports the visible window as a Perfetto trace, exports results JSON, and shows the exact configs behind this run.',
    run: async (h) => { h.highlight('#btn-engineer'); h.openDrawer(false); } },
];
const TOUR_LEN = 182;

export class Tour {
  active = false;
  private t0 = 0;
  private idx = -1;
  private paused = false;
  private pauseAt = 0;
  constructor(private host: HTMLElement, private h: TourHost) {}

  start(): void {
    this.active = true;
    this.idx = -1;
    this.t0 = performance.now();
    this.paused = false;
    this.host.classList.add('open');
    this.render(null);
  }

  stop(): void {
    this.active = false;
    this.host.classList.remove('open');
    this.h.highlight(null);
  }

  private elapsed(): number {
    return ((this.paused ? this.pauseAt : performance.now()) - this.t0) / 1000;
  }

  private go(i: number): void {
    const s = STEPS[i];
    this.idx = i;
    this.t0 = performance.now() - s.at * 1000;
    this.render(null);
    void s.run(this.h).then((note) => { if (note) this.render(note); });
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

  private render(note: string | null): void {
    const s = STEPS[Math.max(0, this.idx)];
    this.host.innerHTML = `
      <div class="tour-card">
        <div class="tour-step">${Math.max(1, this.idx + 1)} of ${STEPS.length}</div>
        <div class="tour-title">${s.title}</div>
        <div class="tour-text">${s.text}</div>
        ${note ? `<div class="tour-note">${note}</div>` : ''}
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
      if (this.paused) { this.t0 += performance.now() - this.pauseAt; this.paused = false; }
      else { this.pauseAt = performance.now(); this.paused = true; }
      this.render(null);
    });
  }
}
