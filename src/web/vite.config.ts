// Vite build/dev config for src/web/ (M6 frontend SPA).
// - @ alias → src/web/src/*
// - Proxy /api + /docs + /openapi.json + /health → FastAPI on :8000
// - /api proxy adds an `error` listener so dev-time proxy failures
//   (ECONNRESET / socket hang up — typically when rag-api returns 5xx
//   while uvicorn is closing a keep-alive socket) don't surface as
//   a blank browser "网络错误"; instead we write a minimal 502 JSON
//   body so the frontend axios `normalizeHttpError` can render a
//   real toast. See memory `vite-proxy-5xx-rescue-2026-09-04`.
// - Manual chunk split per Page (lazy-loaded; see docs/前端架构/路由与目录.md §3.2)

import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';

const BACKEND_PORT = process.env.VITE_API_PORT ?? '8000';
const BACKEND_TARGET = `http://localhost:${BACKEND_PORT}`;

/**
 * Rescue hook for the /api dev proxy: when the upstream proxy itself
 * errors (socket hang up, ECONNRESET, parser error — i.e. cases where
 * no response from the backend ever reached the proxy), write a 502
 * JSON body so axios doesn't see ECONNRESET and surface "网络错误".
 *
 * We deliberately do NOT swallow normal upstream 5xx — those still
 * pass through with their original status + body via the default
 * `proxyRes` handling.
 */
function attachProxyErrorRescue(proxy: any): void {
  proxy.on('error', (err: NodeJS.ErrnoException, _req: any, res: any) => {
    if (!res || res.headersSent) return;
    const code = err.code ?? err.message ?? 'unknown';
    try {
      res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({ detail: `proxy_error: ${code}` }));
    } catch {
      // best-effort rescue; if the response stream is gone we silently
      // bail — there's nothing more we can do here.
    }
  });
}

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // All REST endpoints live under /api/v1/*
      '/api': {
        target: BACKEND_TARGET,
        changeOrigin: true,
        secure: false,
        configure: attachProxyErrorRescue,
      },
      // FastAPI defaults — proxied for dev-time Swagger / OpenAPI inspection
      '/openapi.json': { target: BACKEND_TARGET, changeOrigin: true },
      '/docs': { target: BACKEND_TARGET, changeOrigin: true },
      '/redoc': { target: BACKEND_TARGET, changeOrigin: true },
      '/health': { target: BACKEND_TARGET, changeOrigin: true },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus'],
          'echarts': ['echarts', 'vue-echarts'],
          'vue-vendor': ['vue', 'vue-router', 'pinia', 'vue-i18n'],
        },
      },
    },
  },
  define: {
    // Align import.meta.env type augmentation
    __VITE_APP_TS__: JSON.stringify(new Date().toISOString()),
  },
});