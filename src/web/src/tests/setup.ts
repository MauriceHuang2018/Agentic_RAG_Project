// vitest setup — install Pinia + i18n + Element Plus globally per spec.
//
// httpClient is NOT stubbed at the adapter level: most tests should mock
// the @/api/endpoints/* modules entirely (see auth.spec.ts). When a spec
// needs raw axios control, it can override per-test.

import { beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import type { Plugin } from 'vue';
import { ElButton, ElInput, ElIcon } from 'element-plus';

import { i18n } from '@/i18n';

// Minimal Element Plus stub plugin — registers only the components used
// in component-level specs (el-input for ChatInput, el-button for both).
// Avoids pulling the full Element Plus module into jsdom, which trips on
// directives (v-loading etc.) that touch `document.body` during install.
const elementPlusStub: Plugin = {
  install(app): void {
    app.component('ElButton', ElButton);
    app.component('ElInput', ElInput);
    app.component('ElIcon', ElIcon);
  },
};

beforeEach(() => {
  setActivePinia(createPinia());
  // Touch i18n so its `useI18n()` reads find a non-null instance during tests.
  void i18n;
});

export const testPlugins = [i18n, elementPlusStub] as const;