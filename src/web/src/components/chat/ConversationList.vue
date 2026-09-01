<!--
  ConversationList · sidebar list of past conversations for the current user.
  Backend /conversations endpoint does NOT exist yet (Explore agent 2026-09-01);
  list is fed from props.conversations (local stack) until backend lands.
-->
<template>
  <div class="conversation-list">
    <div class="conversation-list-header">
      <el-button type="primary" plain size="small" @click="emit('new-chat')">
        {{ t('chat.newChat') }}
      </el-button>
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
.conversation-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #fafbfc;
  border-right: 1px solid #ebeef5;
}
.conversation-list-header {
  padding: 12px;
  border-bottom: 1px solid #ebeef5;
}
.conversation-item {
  padding: 12px 16px;
  cursor: pointer;
  border-bottom: 1px solid #f0f0f0;
  transition: background 0.15s;
}
.conversation-item:hover {
  background: #f0f4ff;
}
.conversation-item.is-active {
  background: #e6f1ff;
}
.conversation-item-title {
  font-weight: 500;
  font-size: 14px;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-item-meta {
  font-size: 12px;
  color: #909399;
}
.conversation-empty {
  padding: 24px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
</style>