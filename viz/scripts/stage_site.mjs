// Prune and verify viz/dist after `vite build` so it is ready for a static host.
//
// - keeps only the replays listed in public/replays/index.json, and refuses synthetic ones
// - keeps only the scenes those replays reference
// - copies results/figures (png, svg) next to results/*.json
// - adds .nojekyll for GitHub Pages
// - fails if any file is 25 MiB or larger (Cloudflare Pages per-file limit) or the site has 20,000+ files
// - prints totals per area
import { cpSync, existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';

const viz = new URL('..', import.meta.url).pathname;
const dist = join(viz, 'dist');
const repo = resolve(viz, '..');
const MAX_FILE = 25 * 1024 * 1024;
const MAX_FILES = 20000;

function die(msg) { console.error(`build:site failed: ${msg}`); process.exit(1); }
if (!existsSync(join(dist, 'index.html'))) die('dist/index.html is missing, run vite build first');

// replays
const repDir = join(dist, 'replays');
const index = JSON.parse(readFileSync(join(repDir, 'index.json'), 'utf8'));
const listed = new Set();
const scenes = new Set();
for (const e of index.replays) {
  if (e.synthetic) die(`index.json lists synthetic replay ${e.id}`);
  const mPath = join(repDir, e.path);
  if (!existsSync(mPath)) die(`listed replay ${e.id} has no manifest at replays/${e.path}`);
  const m = JSON.parse(readFileSync(mPath, 'utf8'));
  if (m.synthetic) die(`manifest for ${e.id} says synthetic`);
  listed.add(e.path.split('/')[0]);
  const sc = m.robot?.scene;
  if (sc) {
    const abs = resolve(join(repDir, e.path, '..'), sc);
    const rel = relative(join(dist, 'scenes'), abs);
    if (rel.startsWith('..')) die(`${e.id} points at a scene outside scenes/: ${sc}`);
    scenes.add(rel.split('/')[0]);
  }
}
const removed = [];
for (const name of readdirSync(repDir)) {
  const p = join(repDir, name);
  if (statSync(p).isDirectory() && !listed.has(name)) { rmSync(p, { recursive: true, force: true }); removed.push(`replays/${name}`); }
}
for (const id of listed) if (!existsSync(join(repDir, id))) die(`replays/${id} is missing from dist`);

// scenes
const scDir = join(dist, 'scenes');
if (existsSync(scDir)) {
  for (const name of readdirSync(scDir)) {
    if (!scenes.has(name)) { rmSync(join(scDir, name), { recursive: true, force: true }); removed.push(`scenes/${name}`); }
  }
}
for (const s of scenes) if (!existsSync(join(scDir, s, 'scene.json'))) die(`scene ${s} is referenced but missing from dist/scenes`);

// results figures
const figSrc = join(repo, 'results', 'figures');
let nfig = 0;
if (existsSync(figSrc)) {
  for (const name of readdirSync(figSrc)) {
    if (/\.(png|svg)$/i.test(name)) { cpSync(join(figSrc, name), join(dist, 'results', 'figures', name)); nfig++; }
  }
}

// surrogate
const surMeta = join(dist, 'surrogate', 'meta.json');
if (existsSync(surMeta)) {
  const meta = JSON.parse(readFileSync(surMeta, 'utf8'));
  if (!existsSync(join(dist, 'surrogate', meta.model))) die(`surrogate model ${meta.model} is missing`);
}

writeFileSync(join(dist, '.nojekyll'), '');

// verify sizes and print totals
const areas = new Map();
let total = 0, count = 0;
const tooBig = [];
let largest = { f: '', n: 0 };
function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) { walk(p); continue; }
    const rel = relative(dist, p);
    const area = rel.includes('/') ? rel.split('/')[0] : '(root)';
    const a = areas.get(area) ?? { bytes: 0, files: 0 };
    a.bytes += st.size; a.files++; areas.set(area, a);
    total += st.size; count++;
    if (st.size >= MAX_FILE) tooBig.push(`${rel} (${(st.size / 1048576).toFixed(1)} MiB)`);
    if (st.size > largest.n) largest = { f: rel, n: st.size };
  }
}
walk(dist);
const mib = (n) => `${(n / 1048576).toFixed(1)} MiB`;
if (removed.length) console.log(`removed from dist: ${removed.join(', ')}`);
console.log(`replays: ${listed.size} real (${[...listed].join(' ')})`);
console.log(`scenes: ${[...scenes].join(' ') || 'none'}; figures: ${nfig}`);
for (const [k, v] of [...areas].sort((x, y) => y[1].bytes - x[1].bytes)) console.log(`  ${k.padEnd(10)} ${mib(v.bytes).padStart(10)}  ${v.files} files`);
console.log(`total: ${mib(total)} in ${count} files; largest ${largest.f} ${mib(largest.n)}`);
if (tooBig.length) die(`files at or over 25 MiB: ${tooBig.join(', ')}`);
if (count >= MAX_FILES) die(`${count} files is over the ${MAX_FILES} file limit`);
console.log('every file is under 25 MiB');
