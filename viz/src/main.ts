import '@fontsource-variable/inter';
import './styles.css';
import { Clock, fmtSpeed, fmtTime } from './clock';
import { DsePanel } from './dse/surrogate';
import { LiveClient } from './live/client';
import { LiveStore } from './live/liveStore';
import { ReplayStore, fetchJson } from './replay/store';
import type { Manifest, ReplayIndex, ReplayIndexEntry } from './replay/types';
import { KIND } from './replay/types';
import { EngineerPanel } from './ui/engineer';
import { Tour } from './ui/tour';
import type { TourHost } from './ui/tour';
import { SelectionCard, dominantCause } from './ui/why';
import { DieView, blockLabel } from './views/die';
import { Drawer } from './views/drawer';
import { DeadlineRings } from './views/rings';
import { RobotView } from './views/robot';
import { Timeline } from './views/timeline';

const $ = <T extends HTMLElement = HTMLElement>(sel: string): T => document.querySelector(sel) as T;

const SCENARIOS: { prefix: string; label: string; live: string }[] = [
  { prefix: 'S1_', label: 'Nominal', live: 'nominal' },
  { prefix: 'S2_', label: 'Second camera', live: 'second_camera' },
  { prefix: 'S3_', label: 'Human enters', live: 'human' },
  { prefix: 'S4_', label: 'Faster conveyor', live: 'conveyor_fast' },
  { prefix: 'S5_', label: 'Hot day', live: 'hot_day' },
  { prefix: 'S6_', label: 'Fifteen at once', live: 'fifteen' },
  { prefix: 'S7_', label: '4K recording', live: 'record_4k' },
  { prefix: 'S8_', label: 'Low voltage', live: 'lower_voltage' },
  { prefix: 'S9_', label: 'Person + 4K', live: 'human_4k' },
];
const LIVE_POLICIES = ['P0_nominal', 'P1_fixed_priority', 'P2_edf', 'P3_race_to_idle', 'P4_pace_to_deadline', 'P6_surrogate_guided'];
const POLICIES = ['P0', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6'];

class App implements TourHost {
  clock = new Clock();
  store: ReplayStore | null = null;
  index: ReplayIndex = { format: '', version: 1, replays: [] };
  robot = new RobotView($('#robot-canvas') as HTMLCanvasElement);
  die = new DieView($('#die-canvas') as HTMLCanvasElement, $('#die-labels'));
  rings = new DeadlineRings($('#rings'));
  timeline = new Timeline($('#timeline') as HTMLCanvasElement);
  drawer = new Drawer($('#drawer-metrics'), $('#dse'));
  dse = new DsePanel($('#dse'));
  card = new SelectionCard($('#selection'));
  engineer = new EngineerPanel($('#engineer'), () => this.store, () => [this.timeline.view0, this.timeline.view1]);
  tour = new Tour($('#tour'), this);
  drawerOpen = false;
  selected = -1;
  live: LiveClient | null = null;
  liveFollow = true;
  private liveApplied: string[] = [];
  private loopStarted = false;
  private last = performance.now();
  private lastDrawer = 0;
  private frameTimes: number[] = [];
  private loadSeq = 0;

  async init(): Promise<void> {
    this.bindUi();
    this.resize();
    window.addEventListener('resize', () => this.resize());
    new ResizeObserver(() => this.resize()).observe($('#stage'));
    const params = new URLSearchParams(location.search);
    const live = params.get('live');
    try {
      this.index = await fetchJson<ReplayIndex>('replays/index.json');
    } catch {
      this.index = { format: 'glasschip-replay-index', version: 1, replays: [] };
    }
    // surrogate metadata: index.json "surrogate.meta" (relative to replays/), else the default location
    const sp = params.get('surrogate');
    const metaPath = sp ? new URL(sp, location.href).href
      : this.index.surrogate?.meta ? new URL(this.index.surrogate.meta, new URL('replays/', location.href)).href
        : (await fetch('surrogate/meta.json', { method: 'HEAD' }).then((r) => r.ok && (r.headers.get('content-type') ?? '').includes('json')).catch(() => false))
          ? new URL('surrogate/meta.json', location.href).href : null;
    this.dse.liveAvailable = !!live;
    this.dse.onRunFull = (ov) => this.liveCmd({ cmd: 'run_config', soc_overrides: ov });
    void this.dse.init(metaPath);
    if (live) { this.startLive(live); return; }
    const direct = params.get('manifest');
    if (direct) {
      const st = new ReplayStore(new URL(direct, location.href).href, null);
      await st.load(new URL(direct, location.href).href, (m) => this.setLoading(m));
      await this.attach(st);
      this.setLoading(null);
      requestAnimationFrame((n) => this.frame(n));
      return;
    }
    const want = params.get('replay');
    const entry = this.index.replays.find((r) => r.id === want) ?? this.index.replays.find((r) => r.default) ?? this.index.replays[0];
    if (!entry) { this.setLoading('No replays found in replays/index.json. Run tools/make_dev_replay.py.'); return; }
    await this.loadReplay(entry.id, false);
    const t = Number(params.get('t'));
    if (Number.isFinite(t) && t > 0) this.clock.seek(t * 1e12);
    requestAnimationFrame((n) => this.frame(n));
  }

  // ------------------------------------------------------------- TourHost
  hasReplay(id: string): boolean { return this.index.replays.some((r) => r.id === id); }
  replays(): ReplayIndexEntry[] { return this.index.replays; }
  private manifestCache = new Map<string, Promise<Manifest | null>>();
  manifestOf(id: string): Promise<Manifest | null> {
    if (this.store?.entry?.id === id) return Promise.resolve(this.store.manifest);
    let p = this.manifestCache.get(id);
    if (!p) {
      const e = this.index.replays.find((r) => r.id === id);
      p = e ? fetchJson<Manifest>(new URL(`replays/${e.path}`, location.href).href).catch(() => null) : Promise.resolve(null);
      this.manifestCache.set(id, p);
    }
    return p;
  }
  surrogateLoaded(): boolean { return this.dse.loaded; }
  firstMissSeconds(): Promise<number | null> {
    const st = this.store;
    if (!st || st.missIndices.length === 0) return Promise.resolve(null);
    let t = Infinity;
    for (const i of st.missIndices) t = Math.min(t, st.jobs.release[i]);
    return Promise.resolve(t / 1e12);
  }
  replayId(): string { return this.store?.entry?.id ?? ''; }
  seekSeconds(s: number): void { this.clock.seek(s * 1e12); }
  setSpeed(s: number): void { this.clock.setSpeed(s); }
  setPlaying(p: boolean): void { this.clock.playing = p; this.clock.emit(); }
  clearSelection(): void { this.select(-1); }
  highlight(selector: string | null): void {
    document.querySelectorAll('.tour-focus').forEach((el) => el.classList.remove('tour-focus'));
    if (selector) document.querySelector(selector)?.classList.add('tour-focus');
  }
  openDrawer(open: boolean, section?: 'dse' | 'power' | 'top'): void {
    this.drawerOpen = open;
    $('#drawer').classList.toggle('open', open);
    $('#btn-drawer').classList.toggle('active', open);
    this.updateShift();
    if (open && section) {
      const target = section === 'dse' ? $('#dse') : section === 'power' ? $('#drawer-metrics').children[1] as HTMLElement : null;
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      else $('#drawer-scroll').scrollTo({ top: 0, behavior: 'smooth' });
    }
    this.resize();
  }
  private insets(): [number, number] {
    return [this.selected >= 0 ? 370 : 0, this.drawerOpen ? 380 : 0];
  }
  updateShift(): void {
    const dp = $('#die-pane').getBoundingClientRect();
    const [l, r] = this.insets();
    // rails take ~130 px on each side; with a card open the left rail is hidden behind it
    const mg = this.rings.margin;
    const left = l > 0 ? l + 10 : mg;
    const right = dp.width - r - mg;
    this.die.fitTo((left + right) / 2, Math.min(right - left - 20, dp.height * 1.02));
    $('#rings').classList.toggle('hide-left', l > 0);
  }
  async selectMiss(taskId: string, afterS: number, why: boolean): Promise<boolean> {
    const st = this.store;
    if (!st) return false;
    const ti = st.manifest.tasks.findIndex((t) => t.id === taskId);
    const ji = st.missIndices.find((i) => st.jobs.task[i] === ti && st.jobs.release[i] >= afterS * 1e12);
    if (ji === undefined) return false;
    await this.select(ji, true);
    if (why) await this.why(ji);
    return true;
  }

  async loadReplay(id: string, keepTime: boolean): Promise<void> {
    const entry = this.index.replays.find((r) => r.id === id);
    if (!entry) return;
    if (this.store?.entry?.id === id) return;
    const seq = ++this.loadSeq;
    const t = this.clock.t;
    this.setLoading(`Loading ${entry.scenario_label} / ${entry.policy_label}`);
    const url = new URL(`replays/${entry.path}`, location.href).href;
    const st = new ReplayStore(url, entry);
    await st.load(url, (msg) => this.setLoading(msg));
    if (seq !== this.loadSeq) return;
    await this.attach(st);
    this.clock.seek(keepTime ? Math.min(t, st.duration) : 0);
    if (!keepTime) this.timeline.setView(0, Math.min(st.duration, 3e12));
    this.setLoading(null);
    const u = new URL(location.href);
    u.searchParams.set('replay', id);
    history.replaceState(null, '', u);
  }

  private async attach(st: ReplayStore): Promise<void> {
    this.store = st;
    this.clock.duration = st.duration;
    this.select(-1);
    await this.robot.setStore(st);
    this.die.setStore(st);
    this.rings.setStore(st);
    this.timeline.setStore(st);
    this.drawer.setStore(st);
    this.dse.deadlinesMs = Object.fromEntries(st.manifest.tasks.filter((x) => x.deadline_us).map((x) => [x.id, x.deadline_us! / 1000]));
    this.renderHeader();
    this.renderTransport();
    this.resize();
  }

  private startLive(url: string): void {
    const lc = new LiveClient();
    this.live = lc;
    document.body.classList.add('is-live');
    lc.onStatus = (s) => {
      if (s === 'open') this.setLoading('Live: waiting for the simulator');
      else if (s !== 'connecting') this.setLoading(`Live server ${s}. Start it with: .venv/bin/python tools/live_server.py`);
      else this.setLoading('Connecting to the live server');
    };
    lc.onHello = (m, applied) => {
      const st = new LiveStore(m);
      this.liveApplied = applied.map((a) => String(a.id ?? a.cmd));
      void this.attach(st).then(() => {
        this.setLoading(null);
        this.liveFollow = true;
        this.clock.playing = true;
        this.renderHeader();
        if (!this.loopStarted) requestAnimationFrame((n) => this.frame(n));
      });
    };
    lc.onBatch = (b) => {
      const st = this.store;
      if (!(st instanceof LiveStore)) return;
      st.ingest(b);
      this.clock.duration = st.duration;
    };
    lc.connect(url);
  }

  private liveCmd(c: Record<string, unknown>): void {
    this.live?.send(c);
    if (c.cmd === 'scenario') { this.liveApplied.push(String(c.id)); this.renderHeader(); }
  }

  // ------------------------------------------------------------- UI
  private bindUi(): void {
    $('#btn-play').addEventListener('click', () => this.clock.toggle());
    $('#btn-step').addEventListener('click', () => this.stepEvent(1));
    $('#btn-back').addEventListener('click', () => this.stepEvent(-1));
    const sp = $('#speed') as HTMLInputElement;
    sp.addEventListener('input', () => this.clock.setSpeed(Math.pow(10, Number(sp.value))));
    document.querySelectorAll<HTMLButtonElement>('[data-speed]').forEach((b) =>
      b.addEventListener('click', () => this.clock.setSpeed(Number(b.dataset.speed))));
    this.clock.on(() => this.renderTransport());
    $('#btn-drawer').addEventListener('click', () => this.openDrawer(!this.drawerOpen));
    $('#btn-heat').addEventListener('click', () => {
      this.die.heatOverlay = !this.die.heatOverlay;
      $('#btn-heat').classList.toggle('active', this.die.heatOverlay);
      $('#legend-volt').hidden = this.die.heatOverlay;
      $('#legend-heat').hidden = !this.die.heatOverlay;
    });
    $('#btn-labels').addEventListener('click', () => {
      this.die.showLabels = !this.die.showLabels;
      $('#btn-labels').classList.toggle('active', this.die.showLabels);
    });
    $('#btn-engineer').addEventListener('click', () => this.engineer.toggle());
    $('#btn-tour').addEventListener('click', () => (this.tour.active ? this.tour.stop() : this.tour.start()));
    $('#btn-reset-cam').addEventListener('click', () => this.die.focusOverview());
    $('#btn-live-follow').addEventListener('click', () => { this.liveFollow = true; });
    $('#btn-live-pause').addEventListener('click', () => {
      const st = this.store;
      this.liveCmd({ cmd: st instanceof LiveStore && st.stats?.paused ? 'resume' : 'pause' });
    });
    const vs = $('#vmin') as HTMLInputElement;
    vs.addEventListener('input', () => { $('#vmin-val').textContent = `${Number(vs.value).toFixed(2)} V`; });
    vs.addEventListener('change', () => this.liveCmd({ cmd: 'vmin', domain: 'npu', v: Number(vs.value) }));
    this.timeline.onUserScrub = () => { if (this.live) this.liveFollow = false; };
    this.rings.onSelect = (task) => this.ringClick(task);
    this.timeline.onSeek = (t) => { if (this.live) this.liveFollow = false; this.clock.seek(t); };
    this.timeline.onSelectJob = (ji) => void this.select(ji, false);
    this.card.onClose = () => this.select(-1);
    this.card.onWhy = (ji) => void this.why(ji);
    this.drawer.onJumpMiss = (dir) => this.jumpMiss(dir);
    this.die.onHover = (id, x, y) => this.hoverTip(id, x, y);
    window.addEventListener('keydown', (e) => {
      if ((e.target as HTMLElement)?.closest('input, textarea, select')) return;
      if (e.code === 'Space') { e.preventDefault(); this.clock.toggle(); }
      else if (e.key === 'ArrowRight') this.stepEvent(1);
      else if (e.key === 'ArrowLeft') this.stepEvent(-1);
      else if (e.key === 'Escape') { this.select(-1); this.engineer.toggle(false); }
      else if (e.key === ']') this.clock.setSpeed(this.clock.speed * 10);
      else if (e.key === '[') this.clock.setSpeed(this.clock.speed / 10);
    });
  }

  private renderHeader(): void {
    const st = this.store!;
    const m = st.manifest;
    if (st instanceof LiveStore) { this.renderLiveHeader(st); return; }
    $('#replay-title').textContent = `${m.scenario_label}`;
    $('#replay-policy').textContent = prettyPolicy(m.policy_label || m.policy);
    $('#badge-synthetic').hidden = !m.synthetic;
    const cur = st.entry;
    const sc = $('#scenarios');
    sc.innerHTML = '';
    for (const s of SCENARIOS) {
      const avail = this.index.replays.filter((r) => r.scenario.startsWith(s.prefix))
        .sort((a, b) => Number(a.synthetic) - Number(b.synthetic));
      const b = document.createElement('button');
      b.className = 'seg';
      b.textContent = s.label;
      b.disabled = avail.length === 0;
      b.title = avail.length ? `Switch to ${avail[0].scenario_label}` : 'No replay for this scenario yet';
      if (cur && cur.scenario.startsWith(s.prefix)) b.classList.add('on');
      b.addEventListener('click', () => {
        const samePolicy = avail.find((r) => r.policy === cur?.policy) ?? avail[0];
        if (samePolicy) void this.loadReplay(samePolicy.id, true);
      });
      sc.appendChild(b);
    }
    const pol = $('#policies');
    pol.innerHTML = '';
    let forScenario = this.index.replays.filter((r) => r.scenario === cur?.scenario);
    // when real replays exist for this scenario, older synthetic dev replays are not offered
    if (forScenario.some((r) => !r.synthetic)) forScenario = forScenario.filter((r) => !r.synthetic || r.id === cur?.id);
    for (const p of POLICIES) {
      const e = forScenario.filter((r) => r.policy.startsWith(p + '_'));
      if (e.length === 0) {
        const b = document.createElement('button');
        b.className = 'chip';
        b.textContent = p;
        b.disabled = true;
        b.title = 'No precomputed replay for this policy in this scenario';
        pol.appendChild(b);
        continue;
      }
      for (const r of e) {
        const b = document.createElement('button');
        b.className = 'chip';
        b.textContent = r.policy_label;
        b.title = `${r.policy_label} (seed ${r.seed})`;
        if (r.id === cur?.id) b.classList.add('on');
        b.addEventListener('click', () => void this.loadReplay(r.id, true));
        pol.appendChild(b);
      }
    }
  }

  private renderLiveHeader(st: LiveStore): void {
    $('#replay-title').textContent = 'Live co-simulation';
    $('#replay-policy').textContent = prettyPolicy(st.stats?.policy ?? st.manifest.policy);
    $('#badge-synthetic').hidden = true;
    const sc = $('#scenarios');
    sc.innerHTML = '';
    for (const s of SCENARIOS) {
      const b = document.createElement('button');
      b.className = 'seg';
      b.textContent = s.label;
      b.title = s.live === 'nominal' ? 'Return to the nominal task set, speed and ambient' : `Apply "${s.label}" to the running simulation now`;
      if (this.liveApplied.includes(s.live)) b.classList.add('on');
      b.addEventListener('click', () => {
        if (s.live === 'nominal') this.liveApplied = [];
        this.liveCmd({ cmd: 'scenario', id: s.live });
      });
      sc.appendChild(b);
    }
    const pol = $('#policies');
    pol.innerHTML = '';
    const cur = st.stats?.policy ?? st.manifest.policy;
    for (const p of LIVE_POLICIES) {
      const b = document.createElement('button');
      b.className = 'chip';
      b.textContent = p.split('_')[0];
      b.title = prettyPolicy(p);
      if (p === cur) b.classList.add('on');
      b.addEventListener('click', () => this.liveCmd({ cmd: 'policy', id: p }));
      pol.appendChild(b);
    }
  }

  private renderLiveBar(st: LiveStore): void {
    const s = st.stats;
    const el = $('#live-rate');
    if (!s) { el.textContent = 'Waiting for data'; return; }
    const txt = `${s.paused ? 'Paused' : 'Running'} at ${s.rate.toFixed(2)}× real time · ${s.sim_s.toFixed(1)} s simulated`;
    if (el.textContent !== txt) el.textContent = txt;
    el.classList.toggle('slow', s.rate < 0.25);
    $('#btn-live-follow').hidden = this.liveFollow;
    $('#btn-live-pause').textContent = s.paused ? 'Resume sim' : 'Pause sim';
    const pol = st.stats?.policy;
    if (pol && $('#replay-policy').textContent !== prettyPolicy(pol)) this.renderLiveHeader(st);
  }

  private renderTransport(): void {
    $('#btn-play').innerHTML = this.clock.playing
      ? '<svg viewBox="0 0 24 24" width="18" height="18"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>'
      : '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.2-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/></svg>';
    $('#btn-play').setAttribute('aria-label', this.clock.playing ? 'Pause' : 'Play');
    ($('#speed') as HTMLInputElement).value = String(Math.log10(this.clock.speed));
    $('#speed-label').textContent = fmtSpeed(this.clock.speed);
    document.querySelectorAll<HTMLButtonElement>('[data-speed]').forEach((b) =>
      b.classList.toggle('on', Math.abs(Math.log10(Number(b.dataset.speed)) - Math.log10(this.clock.speed)) < 0.05));
  }

  private setLoading(msg: string | null): void {
    const el = $('#loading');
    el.classList.toggle('show', !!msg);
    if (msg) el.querySelector('.loading-text')!.textContent = msg;
  }

  private resize(): void {
    const rp = $('#robot-pane').getBoundingClientRect();
    this.robot.resize(rp.width, rp.height);
    const dp = $('#die-pane').getBoundingClientRect();
    this.die.resize(dp.width, dp.height);
    const tl = $('#timeline').getBoundingClientRect();
    this.timeline.resize(tl.width, tl.height);
    this.updateShift();
  }

  private hoverTip(id: string | null, x: number, y: number): void {
    const tip = $('#hover-tip');
    const st = this.store;
    if (!id || !st) { tip.classList.remove('show'); return; }
    const b = st.manifest.soc.blocks.find((bb) => bb.id === id);
    const d = String(b?.power_domain ?? '');
    const t = this.clock.t;
    const v = st.sample(`volt.${d}`, t), f = st.sample(`freq.${d}`, t), p = st.sample(`power.${d}`, t), T = st.sample(`temp.${d}`, t);
    const fmt = (n: number, s: string, k = 2): string => (Number.isFinite(n) ? `${n.toFixed(k)} ${s}` : '–');
    tip.innerHTML = `<div class="tip-title">${blockLabel(id)}</div>
      <div class="tip-row"><span>Power domain</span><b>${d}</b></div>
      <div class="tip-row"><span>Voltage</span><b>${fmt(v, 'V')}</b></div>
      <div class="tip-row"><span>Frequency</span><b>${fmt(f / 1e9, 'GHz')}</b></div>
      <div class="tip-row"><span>Domain power</span><b>${fmt(p, 'W')}</b></div>
      <div class="tip-row"><span>Temperature</span><b>${fmt(T, '°C', 1)}</b></div>`;
    tip.style.transform = `translate(${x + 16}px, ${y + 12}px)`;
    tip.classList.add('show');
  }

  private ringClick(task: number): void {
    const st = this.store;
    if (!st) return;
    const t = this.clock.t;
    // prefer the most recent missed job of this task, else the latest completed job
    let ji = -1;
    for (const i of st.missIndices) if (st.jobs.task[i] === task && st.jobs.release[i] <= t) ji = i;
    if (ji < 0 || t - st.jobs.release[ji] > 2e12) {
      ji = st.latestJob(task, t);
      while (ji >= 0 && st.jobs.finish[ji] > t) {
        const arr = st.jobsByTask[task];
        const pos = arr.indexOf(ji);
        ji = pos > 0 ? arr[pos - 1] : -1;
      }
    }
    if (ji >= 0) void this.select(ji, true);
  }

  async select(ji: number, zoom = false): Promise<void> {
    this.selected = ji;
    const st = this.store;
    this.updateShift();
    if (ji < 0 || !st) {
      this.timeline.selection = null;
      this.card.hide();
      this.die.clearPath();
      return;
    }
    const crit = await st.critPath(st.jobs.job[ji]);
    if (this.selected !== ji) return;
    this.timeline.selection = { jobIndex: ji, crit };
    this.card.show(st, ji, crit, false);
    if (zoom) {
      this.timeline.zoomToJob(ji);
      this.clock.playing = false;
      this.clock.seek(st.jobs.release[ji] + (Math.max(st.jobs.finish[ji], st.jobs.deadline[ji]) - st.jobs.release[ji]) * 0.98, 700);
    }
  }

  async why(ji: number): Promise<void> {
    const st = this.store;
    if (!st) return;
    const crit = await st.critPath(st.jobs.job[ji]);
    const r0 = st.jobs.release[ji], r1 = st.jobs.finish[ji];
    await st.ensureEvents(r0, r1);
    const routes: { src: number; dst: number }[] = [];
    const jobId = st.jobs.job[ji];
    for (const b of st.loadedBlocks(r0, r1)) {
      for (let i = 0; i < b.n; i++) if (b.kind[i] === KIND.xfer && b.job[i] === jobId) routes.push({ src: b.src[i], dst: b.dst[i] });
    }
    const task = st.manifest.tasks[st.jobs.task[ji]];
    this.die.showCriticalPath(crit ?? [], routes.slice(0, 12), task.color ?? '#ffffff', dominantCause(st, ji));
    this.card.show(st, ji, crit, true);
    this.timeline.zoomToJob(ji);
    this.clock.playing = false;
    this.clock.seek(st.jobs.deadline[ji], 700);
  }

  private jumpMiss(dir: 1 | -1): void {
    const st = this.store;
    if (!st || st.missIndices.length === 0) return;
    const t = this.clock.t;
    const list = st.missIndices;
    let pick = -1;
    if (dir > 0) pick = list.find((i) => st.jobs.release[i] > t + 1e6) ?? list[0];
    else { for (const i of list) if (st.jobs.release[i] < t - 1e9) pick = i; if (pick < 0) pick = list[list.length - 1]; }
    void this.select(pick, true);
  }

  private stepEvent(dir: 1 | -1): void {
    const st = this.store;
    if (!st) return;
    this.clock.playing = false;
    const t = this.clock.t;
    const span = st.manifest.events.chunk_ps;
    let best = dir > 0 ? Infinity : -Infinity;
    for (const b of st.loadedBlocks(t - span, t + span)) {
      for (let i = 0; i < b.n; i++) {
        if (b.kind[i] !== KIND.op && b.kind[i] !== KIND.xfer) continue;
        const x = b.t[i];
        if (dir > 0 && x > t + 0.5 && x < best) best = x;
        if (dir < 0 && x < t - 0.5 && x > best) best = x;
      }
    }
    if (Number.isFinite(best)) {
      this.clock.seek(best);
      const span2 = Math.max(this.timeline.view1 - this.timeline.view0, 1e6);
      if (best < this.timeline.view0 || best > this.timeline.view1) this.timeline.setView(best - span2 / 2, best + span2 / 2, 200);
      this.clock.setSpeed(Math.min(this.clock.speed, 1e-4));
    }
    void st.ensureEvents(t - span, t + span);
  }

  private frame(now: number): void {
    this.loopStarted = true;
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    this.frameTimes.push(now);
    if (this.frameTimes.length > 240) this.frameTimes.shift();
    const st0 = this.store;
    if (st0 instanceof LiveStore) {
      // follow the head of the stream with a small lag so chunks are complete when drawn
      if (this.liveFollow && st0.duration > 0) this.clock.seek(Math.max(0, st0.duration - 1.5e11));
      else this.clock.tick(dt, now);
      this.clock.speed = Math.max(1e-5, st0.stats?.rate ?? 1);
      this.renderLiveBar(st0);
    } else {
      this.clock.tick(dt, now);
    }
    const t = this.clock.t;
    // the clock loops: warm the first chunks before the wrap so the restart does not refetch on the hot path
    if (this.store && !(this.store instanceof LiveStore) && this.clock.loop && t > this.clock.duration - 1.5e12) {
      void this.store.ensureEvents(0, 5e11);
      void this.store.ensureRobot(0);
    }
    if (this.store) {
      this.robot.update(t, dt);
      this.die.update(t, this.clock.speed, dt);
      const dp = $('#die-pane').getBoundingClientRect();
      const [li, ri] = this.insets();
      const wasCompact = this.rings.margin;
      this.rings.layout(this.die.dieScreen(dp.width, dp.height), dp.width, dp.height, li, ri);
      if (wasCompact !== this.rings.margin) this.updateShift();
      this.rings.update(t, this.clock.speed, now);
      this.timeline.draw(t, this.clock.playing);
      if (now - this.lastDrawer > 120 && this.drawerOpen) {
        this.drawer.update(t);
        this.lastDrawer = now;
      }
      $('#time-readout').textContent = fmtTime(t);
      this.robotStatus(t);
      $('#particle-count').textContent = this.die.particleCount.toLocaleString('en-US');
    }
    this.tour.tick();
    requestAnimationFrame((n) => this.frame(n));
  }

  private robotKey = '';
  private robotStatus(t: number): void {
    const evs = this.store?.manifest.robot?.events ?? [];
    // co-sim recordings log "placed" (object in the bin) and "exited" (object left the belt unpicked);
    // older dev data logs grasp_success / grasp_miss
    const hasPlaced = evs.some((e) => e.kind === 'placed');
    let ok = 0, miss = 0;
    for (const e of evs) {
      if (e.t_ps > t) continue;
      if (hasPlaced ? e.kind === 'placed' : e.kind === 'grasp_success') ok++;
      else if (e.kind === 'grasp_miss' || e.kind === 'exited') miss++;
    }
    const key = `${ok}:${miss}:${hasPlaced}`;
    if (key === this.robotKey) return;
    this.robotKey = key;
    $('#robot-status').innerHTML = `<span class="pill">${hasPlaced ? 'Placed in bin' : 'Picked'} <b>${ok}</b></span>` +
      `<span class="pill ${miss ? 'bad' : ''}">${hasPlaced ? 'Left the belt' : 'Missed'} <b>${miss}</b></span>`;
  }

  fps(): number {
    const f = this.frameTimes;
    if (f.length < 2) return 0;
    return ((f.length - 1) * 1000) / (f[f.length - 1] - f[0]);
  }
}

/** "P4_pace_to_deadline" -> "P4 Pace to deadline" (labels that are already readable pass through). */
function prettyPolicy(p: string): string {
  if (!p.includes('_')) return p;
  const [id, ...rest] = p.split('_');
  const words = rest.join(' ');
  return `${id} ${words.charAt(0).toUpperCase()}${words.slice(1)}`;
}

const app = new App();
(window as unknown as { __glasschip: App }).__glasschip = app;
void app.init();
export type { ReplayIndexEntry };
