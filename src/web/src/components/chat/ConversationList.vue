<!--
  ConversationList · sidebar list of past conversations for the current user.
  Backend /conversations endpoint does NOT exist yet (Explore agent 2026-09-01);
  list is fed from props.conversations (local stack) until backend lands.
-->
<template>
  <div class="conversation-list">
    <div class="conversation-list-header">
      <button type="button" class="conversation-new-btn" @click="emit('new-chat')">
        {{ t('chat.newChat') }}
      </button>
    </div>

    <el-scrollbar v-if="conversations.length > 0" style="height: calc(100% - 56px)">
      <div
        v-for="conv in conversations"
        :key="conv.id"
        :class="['conversation-item', { 'is-active': conv.id === activeId }]"
        @click="emit('select', conv.id)"
      >
        <div class="conversation-item-title">{{ conv.title || t('chat.emptyHistory') }}</div>
        <div class="conversation-item-meta">
          <span>{{ formatTime(conv.lastActivity) }}</span>
          <span v-if="conv.messageCount > 0">· {{ conv.messageCount }} msg</span>
        </div>
      </div>
    </el-scrollbar>
    <div v-else class="conversation-empty">{{ t('chat.emptyHistory') }}</div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

const { t } = useI18n();

export interface ConversationItem {
  id: string;
  title: string;
  lastActivity: number;
  messageCount: number;
}

interface Props {
  conversations: ConversationItem[];
  activeId?: string | null;
}
defineProps<Props>();

const emit = defineEmits<{
  select: [conversationId: string];
  'new-chat': [];
}>();

function formatTime(ts: number): string {
  const d = new Date(ts);
  const now = Date.now();
  const diffMs = now - ts;
  if (diffMs < 60_000) return '刚刚';
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`;
  if (diffMs < 86_400_000) return `${Math.floor(diffMs / 3_600_000)} 小时前`;
  return d.toLocaleDateString();
}
</script>

<style scoped>
/* PlanA v1.0 visual pass — sidebar: surface-1 + 1px right hairline + accent active state. */
.conversation-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--surface-1);
  border-right: var(--hairline);
}
.conversation-list-header {
  padding: var(--s-3) var(--s-4);
  border-bottom: var(--hairline);
}
.conversation-new-btn {
  width: 100%;
  height: 36px;
  border-radius: var(--r-sm);
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  font-weight: 500;
  font-size: var(--fs-14);
  cursor: pointer;
  font-family: var(--font-body);
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.conversation-new-btn:hover {
  background: var(--accent-strong);
  border-color: var(--accent-strong);
}
.conversation-new-btn:focus-visible {
  outline: 2px solid var(--accent-soft);
  outline-offset: 2px;
}
.conversation-item {
  padding: var(--s-3) var(--s-4);
  cursor: pointer;
  border-bottom: 1px solid var(--border);
  position: relative;
  transition: background var(--transition-fast);
}
.conversation-item:hover {
  background: var(--surface-2);
}
.conversation-item.is-active {
  background: var(--accent-soft);
}
.conversation-item.is-active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent);
}
.conversation-item-title {
  font-weight: 500;
  font-size: var(--fs-14);
  color: var(--text-1);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-item-meta {
  font-size: var(--fs-12);
  color: var(--text-3);
  font-family: var(--font-mono);
}
.conversation-empty {
  padding: var(--s-6);
  text-align: center;
  color: var(--text-3);
  font-size: var(--fs-13);
}
</style>