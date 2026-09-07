// useChatStream — encapsulates the /chat/query call + fake streaming
// (backend has no /chat/stream endpoint yet; see api/stream.ts).

import { reactive, ref } from 'vue';
import { streamChat } from '@/api/stream';
import type { ChatQueryResponse } from '@/api/endpoints/chat';
import type { NormalizedError } from '@/api/errors';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations: import('@/api/endpoints/chat').CitationItem[];
  timestamp: number;
  streaming?: boolean;
  /** Full backend response, present only on the final assistant message. */
  response?: ChatQueryResponse;
}

export function useChatStream() {
  const messages = reactive<ChatMessage[]>([]);
  const isStreaming = ref(false);
  const error = ref<NormalizedError | null>(null);
  let abortController: AbortController | null = null;

  /** Send a query; appends both user + assistant messages to `messages`. */
  async function sendQuery(
    query: string,
    options: { conversationId?: string | null; workspaceId?: string | null } = {},
  ): Promise<ChatMessage | null> {
    error.value = null;
    abortController?.abort();
    abortController = new AbortController();

    // IMPORTANT (2026-09-06): wrap both messages with `reactive(...)` so the
    // closure reference IS the reactive proxy. `messages.push(plainObj)`
    // stores the raw object as-is (Vue's `push` instrumentation calls
    // `toRaw(self).push` directly — see @vue/reactivity arrayInstrumentations),
    // so the only way the closure's later mutations (`assistantMsg.content +=`
    // and the `streaming = false` / id / citations assignments) propagate to
    // the template is to make the closure object itself the proxy. Without
    // this, the chat bubble shows "..." forever with a 200 OK response.
    const userMsg = reactive<ChatMessage>({
      id: `user-${Date.now()}`,
      role: 'user',
      content: query,
      citations: [],
      timestamp: Date.now(),
    });
    messages.push(userMsg);

    const assistantMsg = reactive<ChatMessage>({
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      citations: [],
      timestamp: Date.now(),
      streaming: true,
    });
    messages.push(assistantMsg);

    isStreaming.value = true;
    try {
      let final: ChatQueryResponse | undefined;
      for await (const chunk of streamChat(
        {
          query,
          conversationId: options.conversationId ?? null,
          workspaceId: options.workspaceId ?? null,
        },
        abortController.signal,
      )) {
        if (chunk.done && chunk.final) {
          final = chunk.final;
          break;
        }
        assistantMsg.content += chunk.delta;
      }
      if (final) {
        // Backfill: if `streamChat` never yielded any delta chunks (e.g.
        // answer arrived in a single late burst, signal aborted early,
        // or generator skipped straight to the `done` payload) the
        // bubble would otherwise render the loading dots forever —
        // verified manually 2026-09-04 with a 43-second answer that
        // reached the bubble as a single done-payload. Prefer
        // `final.answer` so the UI is never stuck mid-stream.
        if (!assistantMsg.content && final.answer) {
          assistantMsg.content = final.answer;
        }
        // Backend Pydantic schema uses snake_case (see chat/schema.py).
        // The TS-side `ChatQueryResponse` / `CitationItem` interfaces
        // declared camelCase historically, but the runtime payload is
        // snake_case — read `_id` first, fall back to the camelCase
        // alias if some caller has already normalised it.
        //
        // Citation fields go one step further: we MAP every entry into
        // a fresh camelCase object so downstream consumers
        // (FeedbackModal.vue:110 reads `c.chunkId`,
        // CitationDrawer.vue:23 reads `c.chunkId`) get the shape they
        // expect without each consumer re-implementing the fallback.
        // One normalization point here closes the bug class — verified
        // 2026-09-07 when POST /feedback surfaced 422
        // (string_type) on retrieved_chunks because the frontend
        // mapped `undefined` for all 5 chunk ids.
        const messageId = (final as any).message_id ?? final.messageId;
        assistantMsg.id = messageId;
        const rawCitations = final.citations ?? (final as any).citations ?? [];
        assistantMsg.citations = (rawCitations as any[]).map((c) => ({
          chunkId: c.chunk_id ?? c.chunkId ?? '',
          documentName: c.document_name ?? c.documentName ?? '',
          pageNo: c.page_no ?? c.pageNo ?? null,
          relevanceScore: c.relevance_score ?? c.relevanceScore ?? 0,
        }));
        assistantMsg.response = final;
      }
      assistantMsg.streaming = false;
      return assistantMsg;
    } catch (err) {
      assistantMsg.streaming = false;
      error.value = err as NormalizedError;
      // Surface backend error in the assistant slot rather than dropping it.
      assistantMsg.content = assistantMsg.content || (err as NormalizedError).message;
      throw err;
    } finally {
      isStreaming.value = false;
    }
  }

  function abort(): void {
    abortController?.abort();
    isStreaming.value = false;
  }

  function clear(): void {
    abort();
    messages.splice(0, messages.length);
    error.value = null;
  }

  return { messages, isStreaming, error, sendQuery, abort, clear };
}