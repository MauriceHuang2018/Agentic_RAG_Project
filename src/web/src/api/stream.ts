// Stream wrapper.
//
// Backend status (Explore agent 2026-09-01):
//   POST /api/v1/chat/query returns a synchronous JSON response — there is no
//   SSE/NDJSON endpoint yet. Page 2 "streaming" feel is currently a UX illusion
//   (we render the answer as if it streams by chunking it locally on receipt).
//
// This module exposes a uniform `streamChat()` contract so the View layer
// stays unchanged when the backend adds `POST /api/v1/chat/stream`. Today
// it resolves immediately with the full payload wrapped in a single chunk.

import { httpClient, normalizeHttpError } from './client';
import type { AxiosError } from 'axios';

export interface StreamChunk {
  delta: string;
  done: boolean;
  /** Final metadata, present only when `done === true`. */
  final?: import('./endpoints/chat').ChatQueryResponse;
}

export interface ChatQueryRequest {
  query: string;
  conversationId?: string | null;
  workspaceId?: string | null;
  maxIterations?: number | null;
}

export async function* streamChat(
  request: ChatQueryRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamChunk, void, void> {
  try {
    const res = await httpClient.post<import('./endpoints/chat').ChatQueryResponse>(
      '/chat/query',
      {
        query: request.query,
        conversation_id: request.conversationId ?? null,
        workspace_id: request.workspaceId ?? null,
        max_iterations: request.maxIterations ?? null,
      },
      { signal },
    );
    // Local "fake-stream": emit answer in ~40-char chunks so the UI feels alive.
    const answer = res.data.answer ?? '';
    const chunkSize = 40;
    for (let i = 0; i < answer.length; i += chunkSize) {
      if (signal?.aborted) return;
      yield { delta: answer.slice(i, i + chunkSize), done: false };
    }
    yield { delta: '', done: true, final: res.data };
  } catch (err) {
    // Re-throw as NormalizedError for unified toast handling.
    throw normalizeHttpError(err as AxiosError<{ detail?: unknown }>);
  }
}