<!-- Page · 拒绝访问（路由兜底） -->
<template>
  <div class="error-page">
    <h2>🚫 无权访问</h2>
    <p>您没有权限访问此页面。</p>
    <router-link to="/chat">返回首页</router-link>
  </div>
</template>

<style scoped>
.error-page { padding: 48px; text-align: center; }
</style>