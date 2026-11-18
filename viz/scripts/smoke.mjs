// Headless smoke test: decode replays with the viewer's own decoder code (src/replay/decode.ts,
// loaded through Node's TypeScript type stripping) and check them against their manifests.
//
//   node scripts/smoke.mjs                 # the committed fixture plus every replay in public/replays
//   node scripts/smoke.mjs path/to/replay  # one replay directory
//
// With --dist it also checks that `vite build` produced dist/index.html and the worker chunk.
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const { decodeEvents, decodeJobs, indexEvents } = await import(join(root, 'src/replay/decode.ts'));

let failures = 0;
const fail = (msg) => { failures++; console.error(`  FAIL ${msg}`); };
const ok = (cond, msg) => { if (!cond) fail(msg); };

function load(path) {
  const b = readFileSync(path);
  const raw = b[0] === 0x1f && b[1] === 0x8b ? gunzipSync(b) : b;
  return raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
}

function checkReplay(dir) {
  const t0 = performance.now();
  const m = JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf8'));
  console.log(`${dir}  (${m.name}${m.synthetic ? ', synthetic' : ''})`);
  ok(m.format === 'glasschip-replay' && m.version === 1, 'format/version');
  ok(typeof m.synthetic === 'boolean', 'synthetic flag present');
  ok(m.provenance && m.provenance.script, 'provenance.script present');
  const nTasks = m.tasks.length, nStrings = m.events.strings.length, nBlocks = m.soc.blocks.length;
  let total = 0, prevChunkEnd = -1;
  for (const c of m.events.chunks) {
    const blk = indexEvents(decodeEvents(load(join(dir, c.file))));
    ok(blk.n === c.count, `${c.file}: ${blk.n} records, manifest says ${c.count}`);
    total += blk.n;
    for (let i = 0; i < blk.n; i++) {
      if (blk.t[i] < c.t0_ps || (blk.t[i] >= c.t1_ps && c.t1_ps < m.duration_ps)) { fail(`${c.file}: record ${i} outside its chunk`); break; }
      if (i && blk.t[i] < blk.t[i - 1]) { fail(`${c.file}: not sorted at ${i}`); break; }
      if (blk.task[i] !== 0xffff && blk.task[i] >= nTasks) { fail(`${c.file}: task index ${blk.task[i]}`); break; }
      if (blk.op[i] !== 0xffffffff && blk.op[i] >= nStrings) { fail(`${c.file}: op index ${blk.op[i]}`); break; }
      if (blk.block[i] !== 255 && blk.block[i] >= nBlocks) { fail(`${c.file}: block index ${blk.block[i]}`); break; }
      if (blk.t[i] + blk.dur[i] > c.max_end_ps) { fail(`${c.file}: end beyond max_end_ps`); break; }
    }
    ok(c.t0_ps >= prevChunkEnd, `${c.file}: chunk order`);
    prevChunkEnd = c.t0_ps;
  }
  ok(total === m.events.count, `event total ${total} vs manifest ${m.events.count}`);
  const jobs = decodeJobs(load(join(dir, m.jobs.file)), m.jobs.attribution_keys.length);
  ok(jobs.n === m.jobs.count, `jobs ${jobs.n} vs ${m.jobs.count}`);
  let misses = 0;
  for (let i = 0; i < jobs.n; i++) {
    if (jobs.task[i] >= nTasks) { fail(`job ${i} task index`); break; }
    if (jobs.flags[i] & 1) misses++;
  }
  const declared = Object.values(m.results?.tasks ?? {}).reduce((s, t) => s + (t.misses ?? 0), 0);
  ok(!m.results?.tasks || declared === misses, `results.tasks misses ${declared} vs job records ${misses}`);
  const series = load(join(dir, m.series.file));
  for (const s of m.series.items) ok(s.offset + s.count * 4 <= series.byteLength, `series ${s.name} out of range`);
  const lod = load(join(dir, m.lod.file));
  ok(m.lod.busy_bytes === m.lod.bins * m.lod.units.length, 'lod busy size');
  ok(m.lod.link_offset + m.lod.bins * m.lod.links.length * 4 <= lod.byteLength, 'lod link size');
  if (m.robot) {
    let frames = 0;
    for (const f of m.robot.chunks) frames += load(join(dir, f)).byteLength / (m.robot.bodies.length * 7 * 4);
    ok(frames === m.robot.frames, `robot frames ${frames} vs ${m.robot.frames}`);
  }
  console.log(`  ${total.toLocaleString('en-US')} events, ${jobs.n.toLocaleString('en-US')} jobs, ${misses} misses, ${m.series.items.length} series, ${(performance.now() - t0).toFixed(0)} ms`);
}

const args = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const targets = args.length ? args : [join(root, 'tests/fixtures/tiny')];
if (!args.length) {
  const pub = join(root, 'public/replays');
  if (existsSync(pub)) for (const d of readdirSync(pub)) if (existsSync(join(pub, d, 'manifest.json'))) targets.push(join(pub, d));
}
for (const t of targets) checkReplay(t);

if (process.argv.includes('--dist')) {
  const dist = join(root, 'dist');
  ok(existsSync(join(dist, 'index.html')), 'dist/index.html exists');
  const assets = existsSync(join(dist, 'assets')) ? readdirSync(join(dist, 'assets')) : [];
  ok(assets.some((f) => f.startsWith('worker')), 'worker chunk in dist/assets');
  ok(assets.some((f) => f.endsWith('.woff2')), 'self-hosted font in dist/assets');
}

if (failures) {
  console.error(`smoke: ${failures} failure(s)`);
  process.exit(1);
}
console.log(`smoke: ok (${targets.length} replay${targets.length === 1 ? '' : 's'})`);
