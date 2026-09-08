<!--
  ChatInput · textarea + send button. Emits `submit(query)` on enter / click.
  Disabled while parent is streaming.

  The leftmost icon button opens a native multi-file picker; each picked
  file is uploaded in parallel via `uploadDocument`. The input box stays
  editable during uploads (202 returns immediately, parsing happens in
  Celery). Per-file success/failure surfaces as an ElMessage toast; the
  upload icon does not affect streaming state.
-->
<template>
  <div class="chat-input">
    <div class="chat-input-row">
      <!-- Upload icon: triggers the hidden file picker. The icon is an
           inline SVG (upload glyph — arrow rising into a tray) to avoid
           pulling in @element-plus/icons-vue as a new dependency. -->
      <button
        type="button"
        class="chat-input-upload"
        :title="t('chat.upload.tooltip')"
        :aria-label="t('chat.upload.tooltip')"
        :disabled="disabled"
        @click="openPicker"
      >
        <svg
          class="chat-input-upload-icon"
          viewBox="0 0 24 24"
          width="20"
          height="20"
          aria-hidden="true"
          focusable="false"
        >
          <path
            fill="currentColor"
            d="M12 3a1 1 0 0 0-.7.29l-4 4a1 1 0 1 0 1.4 1.42L11 6.41V14a1 1 0 1 0 2 0V6.41l2.3 2.3a1 1 0 1 0 1.4-1.42l-4-4A1 1 0 0 0 12 3Zm-7 14a1 1 0 0 0-1 1v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2a1 1 0 1 0-2 0v2H5v-2a1 1 0 0 0-1-1Z"
          />
        </svg>
      </button>
      <input
        ref="fileInputRef"
        type="file"
        class="chat-input-upload-hidden"
        multiple
        :accept="ACCEPT_ATTR"
        @change="onFilesPicked"
      />
      <el-input
        v-model="text"
        type="textarea"
        :rows="3"
        :placeholder="t('chat.placeholder')"
        :disabled="disabled"
        resize="none"
        @keydown.enter.exact.prevent="onSubmit"
      />
    </div>
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
import { ElMessage } from 'element-plus';
import { useI18n } from 'vue-i18n';
import {
  SUPPORTED_EXTENSIONS,
  isSupportedFile,
  uploadDocument,
} from '@/api/endpoints/documents';

const { t } = useI18n();

interface Props {
  disabled?: boolean;
}
const props = withDefaults(defineProps<Props>(), { disabled: false });

const emit = defineEmits<{ submit: [query: string] }>();

const text = ref('');
/** Hidden file <input> element toggled by the icon button. */
const fileInputRef = ref<HTMLInputElement | null>(null);
/** Mirrors SUPPORTED_EXTENSIONS into the native picker accept attr. */
const ACCEPT_ATTR = SUPPORTED_EXTENSIONS.join(',');

const canSend = computed(() => text.value.trim().length > 0);

function onSubmit(): void {
  if (props.disabled) return;
  const q = text.value.trim();
  if (!q) return;
  emit('submit', q);
  text.value = '';
}

/** Programmatically open the hidden file picker. */
function openPicker(): void {
  fileInputRef.value?.click();
}

/**
 * Invoked when the hidden file input fires `change`. Filters out
 * unsupported extensions (no wasted backend round trip), then runs the
 * accepted files in parallel. Per-file success / failure surfaces as an
 * ElMessage. Always clears the input value so re-picking the same file
 * fires `change` again.
 */
async function onFilesPicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = input.files ? Array.from(input.files) : [];
  // Clear immediately so re-selecting the same file later still fires change.
  input.value = '';
  if (files.length === 0) return;

  const supported: File[] = [];
  const skipped: File[] = [];
  for (const f of files) {
    if (isSupportedFile(f.name)) supported.push(f);
    else skipped.push(f);
  }
  for (const f of skipped) {
    ElMessage.warning(t('chat.upload.unsupported', { name: f.name }));
  }
  if (supported.length === 0) return;

  await Promise.allSettled(
    supported.map(async (file) => {
      try {
        await uploadDocument(file);
        ElMessage.success(t('chat.upload.success', { name: file.name }));
      } catch (err: unknown) {
        const message = (err as { message?: string })?.message ?? 'upload failed';
        ElMessage.error(t('chat.upload.failed', { name: file.name, message }));
      }
    }),
  );
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
.chat-input-row {
  display: flex;
  align-items: flex-end;
  gap: var(--s-3);
}
/* Upload icon button — square hit area, ghost style so it does not
   compete with the primary send button visually. */
.chat-input-upload {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: var(--hairline);
  border-radius: var(--r-sm);
  background: var(--surface-1);
  color: var(--text-2);
  cursor: pointer;
  transition: color 120ms ease, border-color 120ms ease, background 120ms ease;
}
.chat-input-upload:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent-soft);
  background: var(--surface-2);
}
.chat-input-upload:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
/* Inline SVG icon inherits the button's text colour (currentColor). */
.chat-input-upload-icon {
  display: block;
}
/* The native picker is rendered once and toggled via .click(); keep it
   out of the layout flow so it never visibly shifts the composer. */
.chat-input-upload-hidden {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
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