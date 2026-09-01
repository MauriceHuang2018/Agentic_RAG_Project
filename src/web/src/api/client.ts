// HTTP client — axios instance with JWT injection and 401/403 normalization.
//
// Backend contract (see api_gateway/router.py:44-86 + dependencies.py):
//   - Base path: /api/v1 (gateway prefix; Vite proxy strips /api → FastAPI).
//   - Auth: Bearer JWT in Authorization header.
//   - Errors: FastAPI HTTPException → { detail: "<string>" } body.
//     401 invalid_credentials / invalid_token / missing_bearer_token
//     403 user_disabled / permission_denied:<key> / guardrail_block:<cat> (<rule>)
//   - No CORS middleware (Explore agent 2026-09-01) — Vite proxy is the
//     only sanctioned dev path; same-origin in production.

import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios';
import { useAuthStore } from '@/stores/auth';
import { useWorkspaceStore } from '@/stores/workspace';
import { i18n } from '@/i18n';

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? '/api/v1';
const TOKEN_HEADER = 'Authorization';

export interface NormalizedError {
  status: number;
  code: string;
  message: string;
  /** Optional guardrail rule (e.g. 'sensitive_word' | 'pii' | ...). */
  guardrailRule?: string;
  /** Raw backend `detail` payload for debugging. */
  detail?: unknown;
}

export const httpClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30_000,
  headers: {
    Accept: 'application/json',
  },
});

// ─── Request interceptor: attach JWT + active workspace header ─────────────
httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  const ws = useWorkspaceStore();
  if (auth.accessToken) {
    config.headers.set(TOKEN_HEADER, `Bearer ${auth.accessToken}`);
  }
  if (ws.activeWorkspaceId) {
    config.headers.set('X-Workspace-Id', ws.activeWorkspaceId);
  }
  return config;
});

// ─── Response interceptor: 401 clears session, 403 maps to guardrail text ──
httpClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ detail?: unknown }>) => {
    const normalized = normalizeHttpError(error);
    if (normalized.status === 401) {
      const auth = useAuthStore();
      auth.clear();
      // Bounce to /login on next tick; preserves current location.
      if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
        const redirect = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.assign(`/login?redirect=${redirect}`);
      }
    }
    return Promise.reject(normalized);
  },
);

/** Convert axios error → app-wide NormalizedError; t() applied for message. */
export function normalizeHttpError(error: AxiosError<{ detail?: unknown }>): NormalizedError {
  const status = error.response?.status ?? 0;
  const detail = error.response?.data?.detail;
  const t = i18n.global.t;

  if (status === 0) {
    return { status: 0, code: 'network', message: t('errors.network') };
  }
  if (status === 401) {
    return { status, code: 'unauthorized', message: t('errors.unauthorized'), detail };
  }
  if (status === 403) {
    const text = typeof detail === 'string' ? detail : '';
    if (text.startsWith('guardrail_block:')) {
      const rule = text.split(':')[1]?.trim().split(' ')[0] ?? '';
      const i18nKey = `chat.guardrail.${rule}` as const;
      return {
        status,
        code: 'guardrail_block',
        message: t(i18nKey) || t('errors.forbidden'),
        guardrailRule: rule,
        detail,
      };
    }
    if (text === 'user_disabled') {
      return { status, code: 'user_disabled', message: t('login.disabled'), detail };
    }
    return { status, code: 'forbidden', message: t('errors.forbidden'), detail };
  }
  if (status === 404) {
    return { status, code: 'not_found', message: t('errors.notFound'), detail };
  }
  if (status >= 500) {
    return { status, code: 'internal', message: t('errors.internal'), detail };
  }
  const fallback =
    typeof detail === 'string' ? detail : `${status} ${error.response?.statusText ?? ''}`;
  return { status, code: 'unknown', message: fallback || t('errors.internal'), detail };
}