// Times the exported surrogate with onnxruntime-web (wasm backend) under Node, the same
// runtime the viz DSE panel uses. Usage: node surrogate/bench_ortweb.mjs <model.onnx> <n_inputs>
// Each timed run is one slider update: a single design point, every output and its interval.
import fs from 'fs';
const ortUrl = new URL('../viz/node_modules/onnxruntime-web/dist/ort.wasm.min.mjs', import.meta.url);
const ort = await import(ortUrl.href);
const buf = fs.readFileSync(process.argv[2]);
ort.env.wasm.numThreads = 1;
const t_load = performance.now();
const s = await ort.InferenceSession.create(buf, { executionProviders: ['wasm'] });
const load_ms = performance.now() - t_load;
const n = Number(process.argv[3] || 20);
// VX-1 default design (dataset.INPUTS order), S1 workload.
const x = new Float32Array([4, 64, 2, 8, 0, 0, 1, 136, 8, 2, 2, 0.75, 0.75, 0.75, 0.75, 1, 0, 0, 0.2, 25].slice(0, n));
let r = await s.run({ x: new ort.Tensor('float32', x, [1, n]) });
const times = [];
for (let i = 0; i < 300; i++) {
  x[7] = 64 + (i % 140);          // sweep DRAM bandwidth like a slider drag
  const t0 = performance.now();
  r = await s.run({ x: new ort.Tensor('float32', x, [1, n]) });
  times.push(performance.now() - t0);
}
times.sort((a, b) => a - b);
const q = (p) => times[Math.min(times.length - 1, Math.floor(p * times.length))];
console.log(JSON.stringify({ runtime: 'onnxruntime-web wasm (Node ' + process.version + ')', load_ms,
  ms_per_run: times.reduce((a, b) => a + b, 0) / times.length, p50_ms: q(0.5), p95_ms: q(0.95), max_ms: times[times.length - 1],
  n_runs: times.length, n_output_tensors: Object.keys(r).length }));
