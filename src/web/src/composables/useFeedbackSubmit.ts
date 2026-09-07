// useFeedbackSubmit — wraps POST /api/v1/feedback.
// Backend contract (Explore agent 2026-09-01):
//   rating is the string 'like' | 'dislike' (NOT 1|5).
//   Unique (message_id, user_id) → backend converts duplicate POSTs into
//   UPDATEs (no 409 returned); we don't expose idempotency on the UI.

import { computed, ref } from 'vue';
import { httpClient } from '@/api/client';
import type {
  SubmitFeedbackRequest,
  SubmitFeedbackResponse,
} from '@/api/endpoints/feedback';
import type { NormalizedError } from '@/api/client';

export function useFeedbackSubmit() {
  const submitting = ref(false);
  const lastResult = ref<SubmitFeedbackResponse | null>(null);
  // IMPORTANT (2026-09-07): hold the full NormalizedError so the UI can
  // branch on `error.status` / `error.code` if it needs to (e.g. 422
  // validation vs 500 internal). Previously we only kept `.message`,
  // which lost the status/code and forced the catch in FeedbackModal to
  // fall back to a generic "Submit failed" toast.
  const error = ref<NormalizedError | null>(null);

  async function submit(req: SubmitFeedbackRequest): Promise<SubmitFeedbackResponse | null> {
    submitting.value = true;
    error.value = null;
    lastResult.value = null;
    try {
      const res = await httpClient.post<SubmitFeedbackResponse>('/feedback', {
        message_id: req.messageId,
        rating: req.rating,
        comment: req.comment ?? null,
        ragas_scores: req.ragasScores ?? null,
        retrieved_chunks: req.retrievedChunks ?? null,
        query: req.query ?? null,
        answer: req.answer ?? null,
        workspace_id: req.workspaceId ?? null,
        reference_year: req.referenceYear ?? null,
      });
      lastResult.value = res.data;
      return res.data;
    } catch (err) {
      // IMPORTANT (2026-09-07): `err` arriving here is ALREADY a
      // NormalizedError (the response interceptor at api/client.ts:60-75
      // ran normalizeHttpError ONCE and rejected with the normalised
      // object). Calling normalizeHttpError again is the root cause of
      // the "网络错误，请稍后重试" double-false-positive: a NormalizedError
      // has no `.response` field, so the second call always hits the
      // `status === 0 → code: 'network'` branch — masking a 422 / 500
      // from the backend as a generic network error. Use `err` directly.
      const ne = err as NormalizedError;
      error.value = ne;
      throw ne;
    } finally {
      submitting.value = false;
    }
  }

  const hasResult = computed(() => lastResult.value !== null);

  return { submitting, lastResult, hasResult, error, submit };
}