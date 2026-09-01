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
        <span class="streaming-indicator">●●●</span>
      </div>
    </div>

    <div v-if="role === 'assistant' && citations.length > 0" class="message-citations">
      <el-button text size="small" @click="emit('open-citations')">
        <span>📄</span>
        <span>{{ t('chat.citation') }} ({{ citations.length }})</span>
      </el-button>
    </div>

    <div v-if="role === 'assistant' && messageId && !streaming" class="message-actions">
      <el-button
        :type="rating === 'like' ? 'success' : 'default'"
        size="small"
        plain
        @click="emit('feedback', 'like')"
      >
        {{ t('chat.feedbackLike') }}
      </el-button>
      <el-button
        :type="rating === 'dislike' ? 'danger' : 'default'"
        size="small"
        plain
        @click="emit('feedback', 'dislike')"
      >
        {{ t('chat.feedbackDislike') }}
      </el-button>
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
.message-bubble {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 16px;
  border-radius: 12px;
  max-width: 80%;
}
.message-bubble--user {
  align-self: flex-end;
  background: #e6f1ff;
}
.message-bubble--assistant {
  align-self: flex-start;
  background: #f5f7fa;
}
.message-meta {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: #909399;
}
.message-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
}
.message-text--streaming {
  color: #909399;
}
.streaming-indicator {
  animation: pulse 1.2s infinite;
  letter-spacing: 2px;
}
@keyframes pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}
.message-citations,
.message-actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
</style>