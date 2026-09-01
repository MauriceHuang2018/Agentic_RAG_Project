/// <reference types="vite/client" />

// Augment ImportMetaEnv with VITE_* keys declared in .env.example.
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_API_PORT: string;
  readonly VITE_APP_TITLE: string;
  readonly VITE_APP_DEFAULT_LOCALE: string;
  readonly VITE_DEMO_MODE: string;
  readonly VITE_ENABLE_CHAT_STREAM: string;
  readonly VITE_JWT_STORAGE_KEY: string;
  readonly VITE_JWT_USER_STORAGE_KEY: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Allow .vue file imports in TypeScript.
declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>;
  export default component;
}