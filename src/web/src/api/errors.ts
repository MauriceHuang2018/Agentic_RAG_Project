// Error → user-facing message adapter.
// Re-exports `NormalizedError` and adds a `toToastText` helper used by views.

import type { NormalizedError } from './client';
import { i18n } from '@/i18n';

export type { NormalizedError } from './client';

/**
 * Convert a normalized error to a toast-friendly text, falling back to the
 * raw `message` for codes not yet covered by i18n keys.
 */
export function toToastText(err: NormalizedError): string {
  return err.message || i18n.global.t('errors.internal');
}

/** Predicate for guardrail 403 (frontend toast branch). */
export function isGuardrailError(err: unknown): err is NormalizedError {
  return Boolean(
    err &&
      typeof err === 'object' &&
      'code' in err &&
      (err as { code?: string }).code === 'guardrail_block',
  );
}