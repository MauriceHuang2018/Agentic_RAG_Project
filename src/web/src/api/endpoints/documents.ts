// Documents endpoints — used by ChatInput upload icon (Page 2 /chat).
//
// Backend contract (api_gateway/documents_router.py:102-149):
//   POST /documents/upload  multipart/form-data with single `file` field
//                           → 202 UploadResponse { document_id, status, name, size_bytes }
//                           ↳ 400 unsupported file extension / empty upload
//                           ↳ 413 file exceeds STORAGE_MAX_UPLOAD_MB
//                           ↳ 401/403 via response interceptor (api/client.ts)
//
// Single-file only — the frontend composes N parallel POSTs in ChatInput
// for the multi-file UX; no /documents/upload-batch endpoint exists yet
// (M6 design §5.2 still single-file). Adding the batch endpoint is a
// separate backend track.

import { httpClient } from '../client';

export interface UploadResponse {
  documentId: string;
  status: string;
  name: string;
  sizeBytes: number;
}

export interface RawUploadResponse {
  document_id: string;
  status: string;
  name: string;
  size_bytes: number;
}

/**
 * Upload a single file via multipart/form-data. Returns the parsed
 * camelCase UploadResponse on success; rejects with NormalizedError on
 * 400/413/401/403/5xx (see api/client.ts::normalizeHttpError).
 *
 * The backend's `accept` is derived from the file extension
 * (pdf/docx/pptx/xlsx/md/txt); the caller is expected to filter on the
 * client to avoid wasted round trips on clearly-unsupported files.
 */
export async function uploadDocument(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append('file', file, file.name);
  const res = await httpClient.post<RawUploadResponse>('/documents/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return {
    documentId: res.data.document_id,
    status: res.data.status,
    name: res.data.name,
    sizeBytes: res.data.size_bytes,
  };
}

/** Client-side filter mirror of backend _SUPPORTED_FORMATS. */
export const SUPPORTED_EXTENSIONS = ['.pdf', '.docx', '.pptx', '.xlsx', '.md', '.txt'] as const;
export type SupportedExtension = (typeof SUPPORTED_EXTENSIONS)[number];

/**
 * Pick the lowercased extension of `name` (including the dot). Returns
 * `null` for filenames without a recognisable suffix. Used by ChatInput
 * to skip unsupported files before issuing the upload request — the
 * backend would 400 anyway, but skipping here avoids noisy ElMessage.
 */
export function getExtension(name: string): string | null {
  const i = name.lastIndexOf('.');
  if (i < 0 || i === name.length - 1) return null;
  return name.slice(i).toLowerCase();
}

/** True if `name` has an extension in SUPPORTED_EXTENSIONS. */
export function isSupportedFile(name: string): boolean {
  const ext = getExtension(name);
  return ext !== null && (SUPPORTED_EXTENSIONS as readonly string[]).includes(ext);
}