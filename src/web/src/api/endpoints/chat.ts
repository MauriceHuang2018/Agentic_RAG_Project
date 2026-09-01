// chat endpoints — Page 2 (Chat).
// Backend contract (Explore agent 2026-09-01):
//   POST /api/v1/chat/query  ChatQueryRequest → ChatQueryResponse (synchronous).
//     No SSE/NDJSON endpoint today; stream wrapper (api/stream.ts) fakes the UX.
//
// Response fields are declared explicitly here (kept in sync with backend
// chat/schema.py:38-90). Once `gen:openapi` produces a richer types.gen.ts,
// this file can be re-exported from there.

export interface ChatQueryRequest {
  query: string;
  conversationId?: string | null;
  workspaceId?: string | null;
  maxIterations?: number | null;
}

export interface CitationItem {
  chunkId: string;
  documentName: string;
  pageNo: number | null;
  relevanceScore: number;
}

export interface ChatStep {
  stepId: string;
  iteration: number;
  node: string;
  action: string;
  detail: string;
  durationMs: number;
}

export type ChatRoute = 'direct' | 'agent' | 'long_context';
export type ChatSource = 'direct' | 'agent' | 'long_context';

export interface ChatQueryResponse {
  conversationId: string;
  messageId: string;
  answer: string;
  citations: CitationItem[];
  steps: ChatStep[];
  route: ChatRoute;
  iterations: number;
  fallbackTriggered: boolean;
  truncatedByMaxIter: boolean;
  refused: boolean;
  redactions: Record<string, number>;
  metadata: Record<string, unknown> & { source: ChatSource };
}