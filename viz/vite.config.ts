import { defineConfig } from 'vite';

// Relative base so the build works from any static host path (Cloudflare Pages, GitHub Pages).
export default defineConfig({
  base: './',
  worker: { format: 'es' },
  build: {
    target: 'es2022',
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
  },
  optimizeDeps: { exclude: ['onnxruntime-web', 'onnxruntime-web/wasm'] },
});
