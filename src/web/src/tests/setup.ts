// vitest setup — install Pinia + i18n globally per spec.
//
// httpClient is NOT stubbed at the adapter level: most tests should mock
// the @/api/endpoints/* modules entirely (see auth.spec.ts). When a spec
// needs raw axios control, it can override per-test.

import { beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { i18n } from '@/i18n';

beforeEach(() => {
  setActivePinia(createPinia());
  // Touch i18n so its `useI18n()` reads find a non-null instance during tests.
  void i18n;
});