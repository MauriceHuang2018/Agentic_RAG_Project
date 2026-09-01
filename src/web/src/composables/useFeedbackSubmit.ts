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
import { normalizeHttpError } from '@/api/client';
import type { AxiosError } from 'axios';

export function useFeedbackSubmit() {
  const submitting = ref(false);
  const lastResult = ref<SubmitFeedbackResponse | null>(null);
  const error = ref<string | null>(null);

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
      const ne = normalizeHttpError(err as AxiosError<{ detail?: unknown }>);
      error.value = ne.message;
      throw ne;
    } finally {
      submitting.value = false;
    }
  }

  const hasResult = computed(() => lastResult.value !== null);

  return { submitting, lastResult, hasResult, error, submit };
}