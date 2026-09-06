// useChatStream — composable contract tests (2026-09-06).
//
// Pins the reactive contract for `messages.push(assistantMsg)` followed by
// `assistantMsg.content += chunk.delta`. The bug closed here (chat bubble
// stuck at "..." with a 200 OK HTTP response): the plain object literal
// was stored in the reactive array as-is (Vue's `push` instrumentation
// bypasses auto-wrap), and `assistantMsg.content +=` mutated the raw
// object — never notifying the reactive proxy that the template was
// reading from. The fix wraps the message object with `reactive(...)`
// before `push`, so the closure reference IS the proxy.
//
// Two tests:
//   1. Structural: after streaming completes, messages[1] holds the
//      concatenated content + id + citations + streaming=false. This is
//      a smoke test for the happy path.
//   2. Reactivity: a `watch` on `messages[1].content` must fire at
//      least once during streaming. Without the `reactive()` wrap, the
//      plain-object mutations are invisible to the proxy and the
//      callback never re-runs — this is the exact symptom that left
//      MessageBubble showing loading dots forever.

import { beforeEach, describe, it, expect, vi } from 'vitest';
import { nextTick, watch } from 'vue';

const { streamChatMock } = vi.hoisted(() => ({
  streamChatMock: vi.fn(),
}));

vi.mock('@/api/stream', () => ({
  streamChat: streamChatMock,
}));

// Late import — must come after vi.mock so the module gets the mock.
import { useChatStream } from '@/composables/useChatStream';

/** Build a fake /chat/query final payload (snake_case per backend schema). */
function makeFinal(answer: string): {
  conversation_id: string;
  message_id: string;
  answer: string;
  citations: Array<{ chunk_id: string; document_name: string; page_no: number | null; relevance_score: number }>;
  steps: unknown[];
  route: 'direct';
  iterations: number;
  fallback_triggered: boolean;
  truncated_by_max_iter: boolean;
  refused: boolean;
  redactions: Record<string, number>;
  metadata: { source: 'direct' };
} {
  return {
    conversation_id: 'conv-1',
    message_id: 'msg-1',
    answer,
    citations: [
      { chunk_id: 'c1', document_name: 'manual.pdf', page_no: 1, relevance_score: 0.9 },
    ],
    steps: [],
    route: 'direct',
    iterations: 0,
    fallback_triggered: false,
    truncated_by_max_iter: false,
    refused: false,
    redactions: {},
    metadata: { source: 'direct' },
  };
}

/**
 * Mock streamChat to yield a fixed chunk sequence, then the final payload.
 * Async generator yields chunks synchronously back-to-back; useChatStream
 * awaits each one so the watcher gets a chance to fire between chunks.
 */
function mockStreamChunks(chunks: string[], finalAnswer: string): void {
  streamChatMock.mockImplementation(async function* () {
    for (const delta of chunks) {
      yield { delta, done: false };
    }
    yield { delta: '', done: true, final: makeFinal(finalAnswer) as never };
  });
}

describe('useChatStream', () => {
  beforeEach(() => {
    streamChatMock.mockReset();
  });

  it('populates the assistant message after streaming completes', async () => {
    mockStreamChunks(['hello ', 'world', '!'], 'hello world!');

    const { messages, sendQuery } = useChatStream();
    await sendQuery('test query');

    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe('user');
    expect(messages[0].content).toBe('test query');
    expect(messages[0].streaming).toBeUndefined();

    expect(messages[1].role).toBe('assistant');
    expect(messages[1].content).toBe('hello world!');
    expect(messages[1].streaming).toBe(false);
    // Backend snake_case is normalised into the message via the
    // `_id` fallback path in useChatStream (line 87).
    expect(messages[1].id).toBe('msg-1');
    expect(messages[1].citations).toEqual([
      { chunk_id: 'c1', document_name: 'manual.pdf', page_no: 1, relevance_score: 0.9 },
    ]);
  });

  it('content += during streaming triggers reactive watchers (proxy notifies)', async () => {
    mockStreamChunks(['hello ', 'world', '!'], 'hello world!');

    const { messages, sendQuery } = useChatStream();

    // Attach a watcher BEFORE sendQuery so the watcher's effect is
    // established on the array proxy. We capture every re-fire.
    const seen: string[] = [];
    const stop = watch(
      () => messages[1]?.content ?? '',
      (value) => {
        seen.push(value);
      },
    );

    await sendQuery('test query');
    // Drain the watch's flush queue so any pending callbacks have run.
    await nextTick();

    // The watch MUST have fired at least once with a non-empty value
    // during streaming. Before the reactive() wrap fix, += mutations
    // landed on the raw plain object and the proxy never notified —
    // this assertion would fail with `seen.length === 0`.
    expect(seen.length).toBeGreaterThan(0);
    // The final value seen should be the fully concatenated answer.
    expect(seen[seen.length - 1]).toBe('hello world!');

    stop();
  });
});
