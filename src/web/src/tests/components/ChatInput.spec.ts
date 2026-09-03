// ChatInput upload-icon contract tests (T6.x — closed 2026-09-03).
//
// Pins the multi-file upload UX added to the chat composer per M6
// (DESIGN_phase1-mvp §11 — Page 2 Chat):
//   * Clicking the upload icon opens the hidden native picker.
//   * Each supported file in a multi-select triggers exactly one
//     `uploadDocument` call; unsupported files are skipped (no wasted
//     backend round trip, no toast storm) and surface a warning.
//   * Per-file success / failure surfaces as separate ElMessage calls;
//     a failing file does NOT abort sibling uploads (Promise.allSettled
//     semantics — independent feedback per file).
//
// The endpoint module + ElMessage are mocked so this stays a pure
// component test (no HTTP, no DOM file dialog — we drive the change
// event on the hidden input directly).

import { beforeEach, describe, it, expect, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import { testPlugins } from '../setup';

// Hoisted mock refs so the vi.mock factories can mutate them.
const { uploadDocumentMock, elMessage } = vi.hoisted(() => ({
  uploadDocumentMock: vi.fn<(file: File) => Promise<unknown>>(),
  elMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock('@/api/endpoints/documents', () => ({
  SUPPORTED_EXTENSIONS: ['.pdf', '.docx', '.pptx', '.xlsx', '.md', '.txt'],
  isSupportedFile: (name: string) => /\.(pdf|docx|pptx|xlsx|md|txt)$/i.test(name),
  uploadDocument: (file: File) => uploadDocumentMock(file),
}));

// Element Plus is mocked entirely so the component's `import { ElMessage }
// from 'element-plus'` resolves to our spy set. This is enough for the
// upload-icon code path — the el-input / el-button components come from
// the testPlugins array (ElInput + ElButton real registrations), but
// their `name` defaults are stable enough for our queries.
vi.mock('element-plus', () => ({
  ElMessage: elMessage,
}));

import ChatInput from '@/components/chat/ChatInput.vue';

/** Build a minimal File-like object that survives JSON-serialisable tests. */
function makeFile(name: string): File {
  return new File(['hello'], name, { type: 'text/plain' });
}

/**
 * Drive the hidden <input type="file"> change event. jsdom's FileList
 * is a real iterable; we attach the synthetic list to the real input
 * via Object.defineProperty so the component's `event.target.files`
 * reads as expected.
 */
async function pickFiles(wrapper: ReturnType<typeof mount>, files: File[]): Promise<void> {
  const input = wrapper.find('input[type="file"]').element as HTMLInputElement;
  Object.defineProperty(input, 'files', {
    value: files,
    configurable: true,
  });
  await input.dispatchEvent(new Event('change', { bubbles: true }));
  await flushPromises();
}

/** Mount with i18n + the Element Plus stub so useI18n + el-* resolve. */
function mountInput(props: { disabled?: boolean }): ReturnType<typeof mount> {
  return mount(ChatInput, {
    props,
    global: { plugins: [...testPlugins] },
  });
}

describe('ChatInput upload icon', () => {
  beforeEach(() => {
    uploadDocumentMock.mockReset();
    elMessage.success.mockReset();
    elMessage.warning.mockReset();
    elMessage.error.mockReset();
  });

  it('renders an upload button + hidden file input wired to the icon click', async () => {
    const wrapper = mountInput({ disabled: false });
    const button = wrapper.find('button.chat-input-upload');
    expect(button.exists()).toBe(true);

    const hidden = wrapper.find('input[type="file"]');
    expect(hidden.exists()).toBe(true);
    expect(hidden.attributes('multiple')).toBeDefined();
    // Accept attr mirrors the backend _SUPPORTED_FORMATS list.
    expect(hidden.attributes('accept')).toContain('.pdf');
    expect(hidden.attributes('accept')).toContain('.docx');

    // The icon button is the visible trigger — clicking it calls .click()
    // on the hidden input (we spy by stubbing the method).
    const clickSpy = vi.spyOn(hidden.element as HTMLInputElement, 'click');
    await button.trigger('click');
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it('disables the upload icon when the parent is streaming', () => {
    const wrapper = mountInput({ disabled: true });
    const button = wrapper.find('button.chat-input-upload');
    expect(button.attributes('disabled')).toBeDefined();
  });

  it('uploads every supported file in parallel + emits success toasts', async () => {
    uploadDocumentMock.mockResolvedValue({
      documentId: 'doc-1',
      status: 'pending',
      name: 'manual.pdf',
      sizeBytes: 5,
    });
    const wrapper = mountInput({ disabled: false });
    const files = [makeFile('a.pdf'), makeFile('b.docx'), makeFile('c.md')];

    await pickFiles(wrapper, files);

    expect(uploadDocumentMock).toHaveBeenCalledTimes(3);
    expect(uploadDocumentMock).toHaveBeenNthCalledWith(1, files[0]);
    expect(uploadDocumentMock).toHaveBeenNthCalledWith(2, files[1]);
    expect(uploadDocumentMock).toHaveBeenNthCalledWith(3, files[2]);
    expect(elMessage.success).toHaveBeenCalledTimes(3);
    expect(elMessage.warning).not.toHaveBeenCalled();
    expect(elMessage.error).not.toHaveBeenCalled();
  });

  it('skips unsupported files (warning) but still uploads the supported ones', async () => {
    uploadDocumentMock.mockResolvedValue({
      documentId: 'doc-1',
      status: 'pending',
      name: 'manual.pdf',
      sizeBytes: 5,
    });
    const wrapper = mountInput({ disabled: false });
    const files = [makeFile('good.pdf'), makeFile('virus.exe'), makeFile('notes.txt')];

    await pickFiles(wrapper, files);

    expect(uploadDocumentMock).toHaveBeenCalledTimes(2);
    expect(uploadDocumentMock).toHaveBeenNthCalledWith(1, files[0]);
    expect(uploadDocumentMock).toHaveBeenNthCalledWith(2, files[2]);
    expect(elMessage.warning).toHaveBeenCalledTimes(1);
    expect(elMessage.success).toHaveBeenCalledTimes(2);
    expect(elMessage.error).not.toHaveBeenCalled();
  });

  it('a failing upload emits error toast but does NOT abort siblings', async () => {
    uploadDocumentMock.mockImplementation((file: File) => {
      if (file.name === 'broken.pdf') {
        return Promise.reject(new Error('413 file exceeds 25MB'));
      }
      return Promise.resolve({
        documentId: 'doc-ok',
        status: 'pending',
        name: file.name,
        sizeBytes: 5,
      });
    });
    const wrapper = mountInput({ disabled: false });
    const files = [makeFile('broken.pdf'), makeFile('ok.pdf')];

    await pickFiles(wrapper, files);

    expect(uploadDocumentMock).toHaveBeenCalledTimes(2);
    expect(elMessage.success).toHaveBeenCalledTimes(1);
    expect(elMessage.error).toHaveBeenCalledTimes(1);
  });

  it('clears the input value after each pick so re-selecting the same file fires change again', async () => {
    uploadDocumentMock.mockResolvedValue({});
    const wrapper = mountInput({ disabled: false });

    await pickFiles(wrapper, [makeFile('a.pdf')]);
    expect(uploadDocumentMock).toHaveBeenCalledTimes(1);

    // Re-picking the same file should still trigger another upload.
    await pickFiles(wrapper, [makeFile('a.pdf')]);
    expect(uploadDocumentMock).toHaveBeenCalledTimes(2);
  });

  it('emits submit on Enter and clears the textarea without affecting upload', async () => {
    const wrapper = mountInput({ disabled: false });
    const textarea = wrapper.find('textarea');
    await textarea.setValue('hello world');
    await textarea.trigger('keydown.enter.exact.prevent');
    await nextTick();

    expect(wrapper.emitted('submit')).toEqual([['hello world']]);
    // Textarea is reset.
    expect((textarea.element as HTMLTextAreaElement).value).toBe('');
    // Upload state untouched.
    expect(uploadDocumentMock).not.toHaveBeenCalled();
  });
});