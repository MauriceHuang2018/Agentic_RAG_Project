<template>
  <el-config-provider :locale="elementLocale">
    <router-view />
  </el-config-provider>
</template>

<script setup lang="ts">
// Top-level layout for the SPA. Each Page renders its own content inside
// <router-view>; the global Topbar / SidebarNav / WorkspaceSwitcher are
// applied per-route meta in T6.2.

import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import en from 'element-plus/es/locale/lang/en';

const { locale } = useI18n();

const elementLocale = computed(() => (locale.value === 'en-US' ? en : zhCn));
</script>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue',
    Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
</style>