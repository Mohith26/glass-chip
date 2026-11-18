// Playback clock in simulated picoseconds. speed = simulated seconds per wall second.

export type ClockListener = () => void;

export class Clock {
  t = 0;
  duration = 1;
  speed = 1;
  playing = true;
  loop = true;
  private tween: { from: number; to: number; t0: number; ms: number } | null = null;
  private listeners = new Set<ClockListener>();

  /** Advance by a wall-clock delta in seconds. Returns simulated ps advanced. */
  tick(dtWall: number, nowMs: number): number {
    const before = this.t;
    if (this.tween) {
      const k = Math.min(1, (nowMs - this.tween.t0) / this.tween.ms);
      // critically damped feel: ease-out quint
      const e = 1 - Math.pow(1 - k, 5);
      this.t = this.tween.from + (this.tween.to - this.tween.from) * e;
      if (k >= 1) this.tween = null;
    } else if (this.playing) {
      this.t += dtWall * this.speed * 1e12;
      if (this.t >= this.duration) {
        if (this.loop) this.t = 0;
        else { this.t = this.duration; this.playing = false; }
      }
    }
    return this.t - before;
  }

  seek(t: number, animateMs = 0): void {
    const to = Math.max(0, Math.min(this.duration, t));
    if (animateMs > 0) this.tween = { from: this.t, to, t0: performance.now(), ms: animateMs };
    else { this.tween = null; this.t = to; }
    this.emit();
  }

  setSpeed(s: number): void {
    this.speed = Math.max(1e-5, Math.min(4, s));
    this.emit();
  }

  toggle(): void {
    this.playing = !this.playing;
    this.emit();
  }

  on(fn: ClockListener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(): void {
    for (const f of this.listeners) f();
  }
}

/** Damped spring toward a target; call step(dt) per frame. */
export class Spring {
  value: number;
  target: number;
  private v = 0;
  constructor(v0: number, private stiffness = 170, private damping = 26) {
    this.value = v0;
    this.target = v0;
  }
  step(dt: number): number {
    const h = Math.min(dt, 1 / 30);
    const f = -this.stiffness * (this.value - this.target) - this.damping * this.v;
    this.v += f * h;
    this.value += this.v * h;
    return this.value;
  }
  snap(v: number): void {
    this.value = v;
    this.target = v;
    this.v = 0;
  }
}

export function fmtTime(ps: number): string {
  const a = Math.abs(ps);
  if (a >= 1e12) return `${(ps / 1e12).toFixed(3)} s`;
  if (a >= 1e9) return `${(ps / 1e9).toFixed(a >= 1e11 ? 1 : 2)} ms`;
  if (a >= 1e6) return `${(ps / 1e6).toFixed(a >= 1e8 ? 1 : 2)} µs`;
  if (a >= 1e3) return `${(ps / 1e3).toFixed(1)} ns`;
  return `${ps.toFixed(0)} ps`;
}

export function fmtSpeed(s: number): string {
  if (s >= 1) return `${s.toFixed(s >= 2 ? 0 : 1)}×`;
  const inv = 1 / s;
  const r = inv >= 100 ? Math.round(inv / 10 ** (Math.floor(Math.log10(inv)) - 1)) * 10 ** (Math.floor(Math.log10(inv)) - 1) : Math.round(inv);
  return `1/${r.toLocaleString('en-US')}×`;
}

export function fmtNum(v: number, digits = 1): string {
  if (!Number.isFinite(v)) return '–';
  return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
