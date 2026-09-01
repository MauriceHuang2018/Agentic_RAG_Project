<!--
  Page 1 · 登录
  Minimal version for T6.1 dev testing — full layout shell (Topbar etc.)
  ships in M3-1. Form-only view is sufficient to authenticate and reach /chat.

  Backend contract: POST /auth/login  (OAuth2PasswordRequestForm)
  See src/api/endpoints/auth.ts for the typed wrapper.
-->
<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <template #header>
        <h1 class="login-title">{{ t('app.title') }}</h1>
      </template>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        autocomplete="on"
        @submit.prevent="onSubmit"
      >
        <el-form-item :label="t('login.username')" prop="username">
          <el-input
            v-model="form.username"
            name="username"
            autocomplete="username"
            :placeholder="t('login.username')"
          />
        </el-form-item>

        <el-form-item :label="t('login.password')" prop="password">
          <el-input
            v-model="form.password"
            name="password"
            type="password"
            autocomplete="current-password"
            show-password
            :placeholder="t('login.password')"
            @keyup.enter="onSubmit"
          />
        </el-form-item>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          show-icon
          :closable="false"
          class="login-alert"
        />

        <el-button
          type="primary"
          native-type="submit"
          :loading="submitting"
          class="login-submit"
          @click="onSubmit"
        >
          {{ t('login.submit') }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, type FormInstance, type FormRules } from 'element-plus';
import { useI18n } from 'vue-i18n';
import { useAuthStore } from '@/stores/auth';
import { useWorkspaceStore } from '@/stores/workspace';
import { isGuardrailError, type NormalizedError } from '@/api/errors';

/**
 * Sanitize a redirect target to defeat open-redirect attacks.
 * Only single-leading-slash paths are accepted; protocol-relative
 * (`//evil.com`) and backslash variants (`/\\evil.com`) are rejected
 * because browsers may resolve them as external hosts.
 * Falls back to `/chat` when the candidate is missing or unsafe.
 */
function safeRedirect(candidate: unknown): string {
  if (typeof candidate !== 'string') return '/chat';
  if (candidate.length === 0 || candidate.length > 512) return '/chat';
  if (candidate[0] !== '/') return '/chat';
  if (candidate[1] === '/' || candidate[1] === '\\') return '/chat';
  return candidate;
}

const { t } = useI18n();
const auth = useAuthStore();
const ws = useWorkspaceStore();
const router = useRouter();
const route = useRoute();

const formRef = ref<FormInstance>();
const form = reactive({ username: '', password: '' });
const submitting = ref(false);
const errorMessage = ref<string | null>(null);

const rules: FormRules = {
  username: [{ required: true, message: () => t('login.username'), trigger: 'blur' }],
  password: [{ required: true, message: () => t('login.password'), trigger: 'blur' }],
};

async function onSubmit(): Promise<void> {
  if (submitting.value) return;
  errorMessage.value = null;

  const valid = await formRef.value?.validate().catch(() => false);
  if (!valid) return;

  submitting.value = true;
  try {
    await auth.loginWithCredentials(form.username.trim(), form.password);
    // Backend does not yet ship workspace list (Explore agent 2026-09-01);
    // bootstrap with placeholder so chat requests have an active id.
    ws.ensureFallback();
    const redirect = safeRedirect(route.query.redirect);
    await router.replace(redirect);
  } catch (err) {
    // All API failures come through axios → client.ts → NormalizedError.
    // vue-tsc 2.x occasionally narrows catch bindings to `never`; use a
    // fresh object so property reads are type-safe regardless.
    const fallback: NormalizedError = {
      status: 0,
      code: 'unknown',
      message: t('login.failed'),
    };
    const ne: NormalizedError =
      err && typeof err === 'object' && 'status' in err
        ? { ...(err as NormalizedError) }
        : fallback;
    if (isGuardrailError(ne)) {
      // Unlikely on /login but kept symmetric with chat guardrail handling.
      errorMessage.value = ne.message;
    } else if (ne.status === 403 && ne.code === 'user_disabled') {
      errorMessage.value = t('login.disabled');
    } else if (ne.message) {
      errorMessage.value = ne.message;
    } else {
      errorMessage.value = t('login.failed');
    }
    if (errorMessage.value) ElMessage.error(errorMessage.value);
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #f0f4ff 0%, #e8edf7 100%);
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 420px;
}
.login-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  text-align: center;
}
.login-alert {
  margin-bottom: 16px;
}
.login-submit {
  width: 100%;
}
</style>