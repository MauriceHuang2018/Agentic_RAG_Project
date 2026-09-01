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
.chat-input {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}
.chat-input-actions {
  display: flex;
  justify-content: flex-end;
}
</style>