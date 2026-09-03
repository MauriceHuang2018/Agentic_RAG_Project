<!--
  ChatInput · textarea + send button. Emits `submit(query)` on enter / click.
  Disabled while parent is streaming.
-->
<template>
  <div class="chat-input">
    <el-input
      v-model="text"
      type="textarea"
      :rows="3"
      :placeholder="t('chat.placeholder')"
      :disabled="disabled"
      resize="none"
      @keydown.enter.exact.prevent="onSubmit"
    />
    <div class="chat-input-hint">{{ t('chat.inputHint') }}</div>
    <div class="chat-input-actions">
      <el-button
        type="primary"
        :disabled="disabled || !canSend"
        :loading="disabled"
        @click="onSubmit"
      >
        {{ disabled ? t('chat.stop') : t('chat.send') }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';

const { t } = useI18n();

interface Props {
  disabled?: boolean;
}
const props = withDefaults(defineProps<Props>(), { disabled: false });

const emit = defineEmits<{ submit: [query: string] }>();

const text = ref('');

const canSend = computed(() => text.value.trim().length > 0);

function onSubmit(): void {
  if (props.disabled) return;
  const q = text.value.trim();
  if (!q) return;
  emit('submit', q);
  text.value = '';
}
</script>

<style scoped>
/* PlanA v1.0 visual pass — composer with 1px hairline top border + hint row + focus ring. */
.chat-input {
  display: flex;
  flex-direction: column;
  gap: var(--s-2);
  padding: var(--s-4) var(--s-8) var(--s-6);
  background: var(--bg);
  border-top: var(--hairline);
}
.chat-input-hint {
  font-family: var(--font-mono);
  font-size: var(--fs-12);
  color: var(--text-3);
}
.chat-input-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--s-3);
}

/* Element Plus textarea focus ring follows planA accent */
:deep(.el-textarea__inner) {
  resize: none;
  border-radius: var(--r-sm);
  background: var(--surface-1);
  font-family: var(--font-body);
}
:deep(.el-textarea__inner):focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-soft);
}
</style>