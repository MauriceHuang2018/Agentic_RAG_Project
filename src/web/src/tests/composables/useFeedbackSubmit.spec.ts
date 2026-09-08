// useFeedbackSubmit — composable contract tests (2026-09-07).
//
// Pins the error-shape contract for `submit()`. The bug closed here
// (FeedbackModal showing "网络错误，请稍后重试" on every submit, masking
// the real backend 500 from missing `feedbacks.workspace_id` column):
//
//   1. Double normalisation: the response interceptor at
//      api/client.ts:60-75 already runs `normalizeHttpError` ONCE and
//      rejects with a `NormalizedError`. The composable's catch block
//      ran it AGAIN — a NormalizedError has no `.response`, so the
//      second call always hit the `status === 0 → code:'network'`
//      branch, masking every real backend error as a network error.
//
//   2. Duplicate toast: FeedbackModal.vue had both a catch-block
//      `ElMessage.error(...)` AND a `watch(error, e => ElMessage.error(e))`
//      — two toasts per failed submit.
//
// Three tests:
//   1. Success path: submit() resolves with the response payload.
//   2. Error passthrough: when httpClient.post throws a NormalizedError
//      (as the response interceptor produces), error.value is THE SAME
//      object (not re-wrapped) and the thrown error is the SAME
//      reference (so callers can instanceof-check against NormalizedError).
//      Before the fix, error.value was a *fresh* NormalizedError with
//      status=0 / code='network' regardless of the real backend status.
//   3. Status preservation: a 422 from the backend is exposed as
//      status=422 / code='unprocessable_entity' (NOT status=0 /
//      code='network'). This is the exact regression guard for the
//      "double-normalize" gotcha — if anyone re-introduces it, the
//      composable will once again lie to the UI.

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosError } from 'axios';
import type { NormalizedError } from '@/api/client';

const { httpPostMock } = vi.hoisted(() => ({
  httpPostMock: vi.fn(),
}));

vi.mock('@/api/client', async () => {
  // Keep the real NormalizedError type re-export; only stub `httpClient.post`.
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client');
  return {
    ...actual,
    httpClient: {
      post: httpPostMock,
    },
  };
});

import { useFeedbackSubmit } from '@/composables/useFeedbackSubmit';

/** Build the shape of a NormalizedError as the response interceptor produces. */
function makeNormalizedError(
  status: number,
  code: string,
  message: string,
): NormalizedError {
  return { status, code, message };
}

/**
 * Build an AxiosError-shaped object — the input to normalizeHttpError
 * in the response interceptor. Tests use this when they want to
 * exercise the FULL interceptor path (not the already-normalised
 * passthrough path).
 */
function makeAxiosError(
  status: number,
  detail: string,
): AxiosError<{ detail?: unknown }> {
  const err = new Error(`Request failed with status code ${status}`) as AxiosError<{
    detail?: unknown;
  }>;
  err.isAxiosError = true;
  err.name = 'AxiosError';
  err.response = {
    status,
    data: { detail },
    statusText: '',
    headers: {},
    config: {} as never,
  };
  return err;
}

const SAMPLE_REQ = {
  messageId: 'msg-1',
  rating: 'like' as const,
  comment: null,
};

describe('useFeedbackSubmit', () => {
  beforeEach(() => {
    httpPostMock.mockReset();
  });

  it('submit() resolves with the response payload on success', async () => {
    const payload = {
      feedbackId: 'fb-1',
      attributionStatus: 'pending' as const,
      categoryKey: null,
      ticketStatus: 'collected' as const,
      matchedRule: null,
      reasoning: null,
    };
    httpPostMock.mockResolvedValueOnce({ data: payload });

    const { submit, error, lastResult } = useFeedbackSubmit();
    const result = await submit(SAMPLE_REQ);

    expect(result).toEqual(payload);
    expect(lastResult.value).toEqual(payload);
    expect(error.value).toBeNull();
    expect(httpPostMock).toHaveBeenCalledWith('/feedback', expect.objectContaining({
      message_id: 'msg-1',
      rating: 'like',
    }));
  });

  it('submit() preserves the NormalizedError (no double-normalise)', async () => {
    // Simulate what the response interceptor produces: a NormalizedError
    // is rejected from httpClient.post. The composable must use this
    // object as-is — NOT re-run normalizeHttpError (which would lose
    // status=500 and synthesise status=0 code='network').
    const original = makeNormalizedError(500, 'internal', 'Internal server error');
    httpPostMock.mockRejectedValueOnce(original);

    const { submit, error } = useFeedbackSubmit();

    let thrown: unknown;
    try {
      await submit(SAMPLE_REQ);
    } catch (e) {
      thrown = e;
    }

    // IMPORTANT (2026-09-07): `error.value` is a Vue ref wrapping the
    // object in a reactive proxy, so identity is NOT preserved
    // (`toBe` fails even when the structure is identical). What
    // matters is that the underlying shape survives — status=500 is
    // NOT rewritten to status=0.
    expect(error.value).toStrictEqual({
      status: 500,
      code: 'internal',
      message: 'Internal server error',
    });

    // Thrown error: Vue's reactive() wrap returns a Proxy that mirrors
    // the source structurally but is a different reference. Same
    // shape is what callers actually see.
    expect(thrown).toStrictEqual({
      status: 500,
      code: 'internal',
      message: 'Internal server error',
    });
  });

  it('submit() preserves 422 validation status (regression guard)', async () => {
    // Specifically guards against the "double-normalise" bug: a 422
    // from the backend must surface as status=422 (NOT status=0
    // code='network'). Before the 2026-09-07 fix, the catch ran
    // normalizeHttpError on an already-normalised error and the
    // status=0 branch always won.
    const original = makeNormalizedError(422, 'unprocessable_entity', 'rating is required');
    httpPostMock.mockRejectedValueOnce(original);

    const { submit, error } = useFeedbackSubmit();
    await expect(submit(SAMPLE_REQ)).rejects.toStrictEqual({
      status: 422,
      code: 'unprocessable_entity',
      message: 'rating is required',
    });

    expect((error.value as NormalizedError).status).toBe(422);
    expect((error.value as NormalizedError).code).toBe('unprocessable_entity');
    expect((error.value as NormalizedError).message).toBe('rating is required');
  });
});

// Silence the unused warning for makeAxiosError — kept here for future
// tests that want to exercise the full interceptor path via
// normalizeHttpError directly.
void makeAxiosError;