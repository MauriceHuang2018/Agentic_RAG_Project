// Vite build/dev config for src/web/ (M6 frontend SPA).
// - @ alias → src/web/src/*
// - Proxy /api + /docs + /openapi.json + /health → FastAPI on :8000
// - Manual chunk split per Page (lazy-loaded; see docs/前端架构/路由与目录.md §3.2)

import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';

const BACKEND_PORT = process.env.VITE_API_PORT ?? '8000';
const BACKEND_TARGET = `http://localhost:${BACKEND_PORT}`;

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