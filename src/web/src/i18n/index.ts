// vue-i18n bootstrap. Two locales (zh-CN default, en-US fallback).
// i18n key 全清单推迟到 T6.1 实施细节阶段补齐（见 v2.0 §9 TODO #3）。

import { createI18n } from 'vue-i18n';
import zhCN from './locales/zh-CN';
import enUS from './locales/en-US';

const DEFAULT_LOCALE = (import.meta.env.VITE_APP_DEFAULT_LOCALE ?? 'zh-CN') as string;

export const SUPPORTED_LOCALES = ['zh-CN', 'en-US'] as const;
export type LocaleCode = (typeof SUPPORTED_LOCALES)[number];

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: DEFAULT_LOCALE,
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
});