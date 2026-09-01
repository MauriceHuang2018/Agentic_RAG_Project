<!-- Page · 找不到资源（路由兜底） -->
<template>
  <div class="error-page">
    <h2>🔍 找不到页面</h2>
    <p>您访问的页面不存在。</p>
    <router-link to="/chat">返回首页</router-link>
  </div>
</template>

<style scoped>
.error-page { padding: 48px; text-align: center; }
</style>