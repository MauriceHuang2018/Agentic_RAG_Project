<!--
  MessageBubble · renders one chat turn (user or assistant).
  Assistant messages expose citation chips and like/dislike buttons that
  emit events up to Chat.vue (which handles drawer + modal coordination).
-->
<template>
  <div :class="['message-bubble', `message-bubble--${role}`]">
    <div class="message-meta">
      <span class="message-role">{{ roleLabel }}</span>
      <span v-if="timestamp" class="message-time">{{ formatTime(timestamp) }}</span>
    </div>

    <div class="message-content">
      <div v-if="content" class="message-text">{{ content }}</div>
      <div v-else-if="streaming" class="message-text message-text--streaming">
        <span class="streaming-dot" aria-hidden="true"></span>
        <span class="streaming-dot" aria-hidden="true"></span>
        <span class="streaming-dot" aria-hidden="true"></span>
      </div>
    </div>

    <div v-if="role === 'assistant' && citations.length > 0" class="message-citations">
      <div class="message-cite-chip" role="button" tabindex="0" @click="emit('open-citations')">
        <span class="message-cite-chip-rule" aria-hidden="true"></span>
        <span class="message-cite-chip-text">📄 {{ t('chat.citation') }} ({{ citations.length }})</span>
      </div>
    </div>

    <div v-if="role === 'assistant' && messageId && !streaming" class="message-actions">
      <button
        type="button"
        :class="['feedback-btn', rating === 'like' && 'is-like']"
        @click="emit('feedback', 'like')"
      >
        {{ t('chat.feedbackLike') }}
      </button>
      <button
        type="button"
        :class="['feedback-btn', rating === 'dislike' && 'is-dislike']"
        @click="emit('feedback', 'dislike')"
      >
        {{ t('chat.feedbackDislike') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { CitationItem } from '@/api/endpoints/chat';
import type { FeedbackRating } from '@/api/endpoints/feedback';

const { t } = useI18n();

interface Props {
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  timestamp?: number;
  citations?: CitationItem[];
  messageId?: string;
  rating?: FeedbackRating | null;
}
const props = withDefaults(defineProps<Props>(), {
  streaming: false,
  citations: () => [],
  rating: null,
});

const emit = defineEmits<{
  'open-citations': [];
  feedback: [rating: FeedbackRating];
}>();

const roleLabel = computed(() => (props.role === 'user' ? '🧑 You' : '🤖 DocGPT'));

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString();
}
</script>

<style scoped>
/* PlanA v1.0 visual pass — see plan §四 Step 3. Logic/emits unchanged. */
.message-bubble {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  padding: var(--s-4) var(--s-5);
  border-radius: var(--r-md);
  max-width: 720px;
  border: 1px solid var(--border);
  background: var(--surface-1);
  line-height: 1.7;
  font-size: var(--fs-15);
  word-break: break-word;
}
.message-bubble--user {
  align-self: flex-end;
  background: var(--text-1);
  color: var(--text-inverse);
  border-color: var(--text-1);
}
.message-bubble--assistant {
  align-self: flex-start;
  background: var(--surface-1);
  color: var(--text-1);
}
.message-meta {
  display: flex;
  gap: var(--s-2);
  font-size: var(--fs-12);
  color: var(--text-3);
  font-family: var(--font-mono);
}
.message-text {
  white-space: pre-wrap;
  line-height: 1.7;
}
.message-text--streaming {
  color: var(--text-3);
}

/* Typing indicator: 3 dots bouncing (signature element of PlanA §7) */
.streaming-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin: 0 2px;
  border-radius: 50%;
  background: var(--text-3);
  vertical-align: middle;
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.streaming-dot:nth-child(2) { animation-delay: 0.2s; }
.streaming-dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes typing-bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
  40% { transform: translateY(-4px); opacity: 1; }
}

/* Signature citation chip: surface-1 + 1px border + 3px accent left bar (PlanA §7.2) */
.message-citations { display: flex; gap: var(--s-3); margin-top: var(--s-2); }
.message-cite-chip {
  display: grid;
  grid-template-columns: 4px 1fr;
  gap: var(--s-3);
  padding: var(--s-2) var(--s-3);
  background: var(--cite-bg);
  border: 1px solid var(--border);
  border-left: var(--cite-edge);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  font-size: var(--fs-13);
  color: var(--text-2);
  cursor: pointer;
  transition: background var(--transition-fast);
  width: 100%;
  box-sizing: border-box;
}
.message-cite-chip:hover { background: var(--surface-2); }
.message-cite-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.message-cite-chip-rule { background: var(--accent); }
.message-cite-chip-text { line-height: 1.5; }

/* Feedback row: native buttons + planA accent colors */
.message-actions { display: flex; gap: var(--s-3); margin-top: var(--s-2); }
.feedback-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--s-1);
  padding: var(--s-1) var(--s-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--surface-1);
  font-size: var(--fs-12);
  color: var(--text-2);
  cursor: pointer;
  font-family: var(--font-body);
}
.feedback-btn:hover { background: var(--surface-2); }
.feedback-btn.is-like {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent-strong);
}
.feedback-btn.is-dislike {
  background: rgba(220, 38, 38, 0.08);
  border-color: rgba(220, 38, 38, 0.25);
  color: var(--err);
}
</style>