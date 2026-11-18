// Copy repo configs (YAML) into public/configs so the engineer-mode viewer can show them.
import { cpSync, existsSync, mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';

const root = new URL('../../configs/', import.meta.url).pathname;
const out = new URL('../public/configs/', import.meta.url).pathname;
mkdirSync(out, { recursive: true });
const files = [];
function walk(dir) {
  if (!existsSync(dir)) return;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (name.endsWith('.yaml') || name.endsWith('.yml')) {
      const rel = relative(root, p);
      mkdirSync(join(out, rel, '..'), { recursive: true });
      cpSync(p, join(out, rel));
      files.push(rel);
    }
  }
}
walk(root);
writeFileSync(join(out, 'index.json'), JSON.stringify({ files: files.sort() }, null, 1));
console.log(`synced ${files.length} config files`);

// results JSON used by the viewer (latency tolerance band, later the DSE summaries)
const resRoot = new URL('../../results/', import.meta.url).pathname;
const resOut = new URL('../public/results/', import.meta.url).pathname;
mkdirSync(resOut, { recursive: true });
let nres = 0;
if (existsSync(resRoot)) {
  for (const name of readdirSync(resRoot)) {
    if (name.endsWith('.json')) { cpSync(join(resRoot, name), join(resOut, name)); nres++; }
  }
}
console.log(`synced ${nres} results files`);
