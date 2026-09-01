// vitest setup — install Pinia + i18n globally and stub the httpClient.
//
// Keep stubs tiny: each spec should override what it needs via vi.mocked().
// The default stubs prevent real network calls leaking across tests.

import { beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { vi } from 'vitest';

// i18n must load before any store/t uses t() in setup-time derivations.
import { i18n } from '@/i18n';
import { httpClient } from '@/api/client';

beforeEach(() => {
  setActivePinia(createPinia());
  // Reset axios adapter so prior test leakage doesn't poison subsequent runs.
  httpClient.defaults.adapter = vi.fn();
});