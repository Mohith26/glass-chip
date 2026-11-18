// One time source for UI animation. Normal runs use performance.now(); capture mode
// (?capture=1) swaps in a virtual clock so frames can be stepped deterministically.
let source: () => number = () => performance.now();

export function nowMs(): number {
  return source();
}

export function setTimeSource(f: () => number): void {
  source = f;
}
