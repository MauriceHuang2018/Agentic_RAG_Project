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

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: query,
      citations: [],
      timestamp: Date.now(),
    };
    messages.push(userMsg);

    const assistantMsg: ChatMessage = {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      citations: [],
      timestamp: Date.now(),
      streaming: true,
    };
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
        assistantMsg.id = final.messageId;
        assistantMsg.citations = final.citations;
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