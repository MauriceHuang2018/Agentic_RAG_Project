<!--
  Page 2 · 智能问答 (Chat.vue)
  Top-level view: workspace selector + conversation sidebar + message stream
  + input. Delegates rendering to MessageBubble / ChatInput / CitationDrawer
  / ConversationList. Composes useChatStream for the streaming loop and
  useFeedbackSubmit for the feedback modal.
-->
<template>
  <div class="chat-page">
    <header class="chat-header">
      <div class="chat-header-left">
        <h1 class="chat-title">{{ t('nav.chat') }}</h1>
        <el-select
          v-model="activeWorkspace"
          size="small"
          style="width: 200px"
          :disabled="workspaceStore.visibleWorkspaces.length === 0"
          :placeholder="t('chat.workspacePlaceholder')"
          @change="onWorkspaceChange"
        >
          <el-option
            v-for="ws in workspaceStore.visibleWorkspaces"
            :key="ws.id"
            :label="ws.name"
            :value="ws.id"
          />
        </el-select>
      </div>
      <div class="chat-header-right">
        <span class="chat-user">{{ auth.username }}</span>
        <el-button text @click="onLogout">{{ t('nav.logout') }}</el-button>
      </div>
    </header>

    <div class="chat-body">
      <aside class="chat-sidebar">
        <ConversationList
          :conversations="conversations"
          :active-id="activeConversationId"
          @new-chat="onNewChat"
          @select="onSelectConversation"
        />
      </aside>

      <main class="chat-main">
        <el-alert
          v-if="workspaceStore.visibleWorkspaces.length === 0"
          type="warning"
          :title="t('chat.noWorkspaceTitle')"
          :description="t('chat.noWorkspaceDesc')"
          show-icon
          :closable="false"
          class="chat-empty"
        />
        <el-scrollbar
          v-else
          ref="scrollRef"
          class="chat-messages"
        >
          <div class="chat-messages-inner">
            <MessageBubble
              v-for="m in chatMessages"
              :key="m.id"
              :role="m.role"
              :content="m.content"
              :streaming="m.streaming ?? false"
              :timestamp="m.timestamp"
              :citations="m.citations"
              :message-id="m.role === 'assistant' && !m.streaming ? m.id : undefined"
              :rating="ratings[m.id] ?? null"
              @open-citations="openCitations(m.citations)"
              @feedback="(r) => openFeedback(m, r)"
            />
          </div>
        </el-scrollbar>

        <ChatInput
          :disabled="isStreaming || workspaceStore.visibleWorkspaces.length === 0"
          @submit="onSubmitQuery"
        />
      </main>
    </div>

    <CitationDrawer
      v-model:visible="drawerVisible"
      :citations="drawerCitations"
    />

    <FeedbackModal
      v-model:visible="feedbackVisible"
      :message="feedbackTarget"
      :initial-rating="feedbackRating"
      @submitted="onFeedbackSubmitted"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { useI18n } from 'vue-i18n';
import { useAuthStore } from '@/stores/auth';
import { useWorkspaceStore } from '@/stores/workspace';
import { useChatStream, type ChatMessage } from '@/composables/useChatStream';
import { isGuardrailError } from '@/api/errors';
import MessageBubble from '@/components/chat/MessageBubble.vue';
import ChatInput from '@/components/chat/ChatInput.vue';
import CitationDrawer from '@/components/chat/CitationDrawer.vue';
import ConversationList, {
  type ConversationItem,
} from '@/components/chat/ConversationList.vue';
import FeedbackModal from '@/components/chat/FeedbackModal.vue';
import type { CitationItem } from '@/api/endpoints/chat';
import type { FeedbackRating } from '@/api/endpoints/feedback';

const { t } = useI18n();
const auth = useAuthStore();
const workspaceStore = useWorkspaceStore();
const router = useRouter();

const chat = useChatStream();
const { isStreaming, messages: chatMessages } = chat;
const activeWorkspace = ref<string | null>(workspaceStore.activeWorkspaceId);

// ─── Conversation sidebar (local-only, since /conversations endpoint missing) ──
const conversations = ref<ConversationItem[]>([]);
const activeConversationId = ref<string | null>(null);
const ratings = ref<Record<string, FeedbackRating>>({});

// ─── Citation drawer ────────────────────────────────────────────────
const drawerVisible = ref(false);
const drawerCitations = ref<CitationItem[]>([]);

function openCitations(citations: CitationItem[]): void {
  drawerCitations.value = citations;
  drawerVisible.value = true;
}

// ─── Feedback modal ────────────────────────────────────────────────
const feedbackVisible = ref(false);
const feedbackTarget = ref<ChatMessage | null>(null);
const feedbackRating = ref<FeedbackRating | null>(null);

function openFeedback(msg: ChatMessage, rating: FeedbackRating): void {
  feedbackTarget.value = msg;
  feedbackRating.value = rating;
  ratings.value[msg.id] = rating;
  feedbackVisible.value = true;
}

function onFeedbackSubmitted(): void {
  ElMessage.success(t('feedback.submit') + ' ✓');
}

// ─── Chat input ─────────────────────────────────────────────────────
async function onSubmitQuery(query: string): Promise<void> {
  try {
    const reply = await chat.sendQuery(query, {
      workspaceId: activeWorkspace.value,
    });
    if (reply && activeConversationId.value === null) {
      activeConversationId.value = reply.response?.conversationId ?? null;
      upsertConversationFromReply(query, reply);
    } else if (reply) {
      upsertConversationFromReply(query, reply);
    }
  } catch (err: unknown) {
    const ne = err as { message?: string };
    if (isGuardrailError(err)) {
      ElMessage.warning(ne.message ?? 'guardrail_block');
    } else {
      ElMessage.error(ne.message ?? 'Chat failed');
    }
  }
}

function upsertConversationFromReply(query: string, reply: ChatMessage): void {
  const convId = reply.response?.conversationId ?? activeConversationId.value ?? `local-${Date.now()}`;
  const existing = conversations.value.find((c) => c.id === convId);
  const title = query.slice(0, 30);
  if (existing) {
    existing.lastActivity = reply.timestamp;
    existing.messageCount += 2;
  } else {
    conversations.value.unshift({
      id: convId,
      title,
      lastActivity: reply.timestamp,
      messageCount: 2,
    });
  }
  activeConversationId.value = convId;
}

function onNewChat(): void {
  chat.clear();
  activeConversationId.value = null;
  feedbackTarget.value = null;
  feedbackRating.value = null;
}

function onSelectConversation(id: string): void {
  activeConversationId.value = id;
}

async function onWorkspaceChange(next: string | null): Promise<void> {
  workspaceStore.setActive(next);
  chat.clear();
  ElMessage.info(`workspace → ${next}`);
}

async function onLogout(): Promise<void> {
  await auth.logout();
  workspaceStore.clear();
  await router.replace('/login');
}

// ─── Lifecycle ─────────────────────────────────────────────────────
// Sync the dropdown's local ref with the store on mount. If the store
// has no workspace data (e.g. `/me/workspaces` failed during login),
// `activeWorkspace` stays null and the empty-state alert renders above
// the input box — we never fall back to a fake UUID (the old behaviour
// sent bogus chat requests that 403'd every time).
onMounted(() => {
  activeWorkspace.value = workspaceStore.activeWorkspaceId;
});

// Auto-scroll to bottom on new messages.
const scrollRef = ref();
watch(
  () => chatMessages.length,
  async () => {
    await new Promise((r) => setTimeout(r, 50));
    scrollRef.value?.scrollTo?.({ bottom: 0 });
  },
);

</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg);
}
.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--s-5) var(--s-8) var(--s-4);
  border-bottom: var(--hairline);
  background: var(--bg);
}
.chat-header-left,
.chat-header-right {
  display: flex;
  align-items: center;
  gap: var(--s-4);
}
.chat-title {
  margin: 0;
  font-size: var(--fs-22);
  font-weight: 600;
  letter-spacing: var(--ls-display);
  color: var(--text-1);
}
.chat-user {
  font-size: var(--fs-14);
  color: var(--text-2);
}
.chat-body {
  flex: 1;
  display: flex;
  min-height: 0;
  background: var(--bg);
}
.chat-sidebar {
  width: 288px;
  flex-shrink: 0;
  background: var(--surface-1);
  border-right: var(--hairline);
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-messages {
  flex: 1;
  padding: var(--s-6) var(--s-8);
  scroll-behavior: smooth;
}
.chat-empty {
  margin: var(--s-6);
}
.chat-empty :deep(.el-alert) {
  border-radius: var(--r-md);
}
.chat-messages-inner {
  display: flex;
  flex-direction: column;
  gap: var(--s-4);
  max-width: 820px;
  margin: 0 auto;
  width: 100%;
}
</style>