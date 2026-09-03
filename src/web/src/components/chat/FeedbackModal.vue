<!--
  Page 3 · 反馈弹窗 (FeedbackModal.vue)
  el-dialog with like/dislike + comment textarea. On submit, calls
  useFeedbackSubmit and shows the backend's attribution_status as an alert.
-->
<template>
  <el-dialog
    :model-value="visible"
    :title="t('feedback.title')"
    width="480px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:visible', $event)"
  >
    <el-form label-position="top">
      <el-form-item>
        <el-radio-group v-model="rating" size="large">
          <el-radio-button :value="'like'">{{ t('feedback.scoreUp') }}</el-radio-button>
          <el-radio-button :value="'dislike'">{{ t('feedback.scoreDown') }}</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item>
        <el-input
          v-model="comment"
          type="textarea"
          :rows="4"
          :placeholder="t('feedback.commentPlaceholder')"
        />
      </el-form-item>

      <el-alert
        v-if="commentRequired && !comment.trim()"
        :title="t('feedback.commentRequired')"
        type="warning"
        show-icon
        :closable="false"
      />
    </el-form>

    <el-alert
      v-if="lastResult"
      :title="`attribution: ${lastResult.attributionStatus} · category: ${lastResult.categoryKey ?? '-'}`"
      type="success"
      show-icon
      :closable="false"
      class="feedback-result"
    />

    <template #footer>
      <el-button @click="emit('update:visible', false)">{{ t('feedback.cancel') }}</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="onSubmit">
        {{ t('feedback.submit') }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { useI18n } from 'vue-i18n';
import { useFeedbackSubmit } from '@/composables/useFeedbackSubmit';
import type { ChatMessage } from '@/composables/useChatStream';
import type { FeedbackRating } from '@/api/endpoints/feedback';

const { t } = useI18n();

interface Props {
  visible: boolean;
  message: ChatMessage | null;
  initialRating?: FeedbackRating | null;
}
const props = withDefaults(defineProps<Props>(), { initialRating: null });
const emit = defineEmits<{
  'update:visible': [value: boolean];
  submitted: [];
}>();

const rating = ref<FeedbackRating>(props.initialRating ?? 'like');
const comment = ref('');
const { submitting, lastResult, error, submit } = useFeedbackSubmit();

const commentRequired = computed(() => rating.value === 'dislike');
const canSubmit = computed(() => {
  if (!rating.value) return false;
  if (commentRequired.value && !comment.value.trim()) return false;
  return true;
});

// Reset on open.
watch(
  () => props.visible,
  (v) => {
    if (v) {
      rating.value = props.initialRating ?? 'like';
      comment.value = '';
    }
  },
);

async function onSubmit(): Promise<void> {
  if (!props.message) return;
  try {
    await submit({
      messageId: props.message.id,
      rating: rating.value,
      comment: comment.value.trim() || null,
      query: props.message.role === 'assistant' ? '' : props.message.content,
      answer: props.message.role === 'assistant' ? props.message.content : '',
      retrievedChunks: props.message.citations.map((c) => c.chunkId),
    });
    emit('submitted');
    emit('update:visible', false);
  } catch (err) {
    ElMessage.error((err as { message?: string }).message ?? 'Submit failed');
  }
}

// Surface submit error inline.
watch(error, (e) => {
  if (e) ElMessage.error(e);
});
</script>

<style scoped>
/* PlanA v1.0 visual pass — dialog + radio button + result alert surface. */
:deep(.el-dialog) {
  border-radius: var(--r-lg);
}
:deep(.el-dialog__header) {
  padding: var(--s-5) var(--s-6) var(--s-3);
}
:deep(.el-dialog__title) {
  font-family: var(--font-display);
  font-size: var(--fs-18);
  font-weight: 600;
  letter-spacing: var(--ls-display);
  color: var(--text-1);
}
:deep(.el-dialog__body) {
  padding: var(--s-3) var(--s-6) var(--s-5);
}
:deep(.el-dialog__footer) {
  padding: var(--s-3) var(--s-6) var(--s-5);
  border-top: var(--hairline);
}
:deep(.el-radio-button__inner) {
  border-radius: var(--r-sm);
}
.feedback-result {
  margin-top: var(--s-3);
  border-radius: var(--r-md);
}
</style>