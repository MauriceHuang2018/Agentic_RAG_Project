// feedback endpoints — Page 3 (FeedbackModal) + Page 6 (admin feedback list).
// Backend contract (Explore agent 2026-09-01):
//   POST /api/v1/feedback  SubmitFeedbackRequest → SubmitFeedbackResponse (201).
//     rating is the string 'like' | 'dislike' (NOT 1|5 — that's the ORM-only
//     `score` column, not exposed via HTTP).
//     Unique (message_id, user_id) constraint: backend converts duplicate
//     POSTs into UPDATEs (see feedback/repository.py:171-240); HTTP layer
//     does NOT return 409.

export type FeedbackRating = 'like' | 'dislike';

export interface SubmitFeedbackRequest {
  messageId: string;
  rating: FeedbackRating;
  comment?: string | null;
  ragasScores?: Record<string, number> | null;
  retrievedChunks?: string[] | null;
  query?: string | null;
  answer?: string | null;
  workspaceId?: string | null;
  referenceYear?: number | null;
}

export type AttributionStatus = 'pending' | 'succeeded' | 'failed' | 'skipped';
export type FeedbackCategoryKey =
  | 'retrieval'
  | 'chunking'
  | 'generation'
  | 'knowledge'
  | 'user_query'
  | null;
export type TicketStatus =
  | 'collected'
  | 'pending_attribution'
  | 'attributed'
  | 'resolved'
  | 'rejected'
  | null;

export interface SubmitFeedbackResponse {
  feedbackId: string;
  attributionStatus: AttributionStatus;
  categoryKey: FeedbackCategoryKey;
  ticketStatus: TicketStatus;
  matchedRule: string | null;
  reasoning: string | null;
}

export { httpClient as _httpClient } from './client';