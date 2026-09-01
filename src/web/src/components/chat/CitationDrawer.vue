<!--
  CitationDrawer · el-drawer listing the CitationItem array returned by
  /chat/query. Each row shows document name + page + relevance score.
-->
<template>
  <el-drawer
    :model-value="visible"
    :title="t('chat.citation')"
    direction="rtl"
    size="420px"
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-if="citations.length === 0" class="citation-empty">{{ t('common.empty') }}</div>
    <div v-else class="citation-list">
      <div v-for="(c, idx) in citations" :key="c.chunkId" class="citation-item">
        <div class="citation-item-header">
          <strong>[{{ idx + 1 }}]</strong>
          <span class="citation-doc">{{ c.documentName }}</span>
          <el-tag v-if="c.pageNo != null" size="small" type="info">p.{{ c.pageNo }}</el-tag>
          <el-tag size="small" type="success">{{ c.relevanceScore.toFixed(2) }}</el-tag>
        </div>
        <code class="citation-chunk-id">{{ c.chunkId }}</code>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import type { CitationItem } from '@/api/endpoints/chat';

const { t } = useI18n();

interface Props {
  visible: boolean;
  citations: CitationItem[];
}
defineProps<Props>();

const emit = defineEmits<{ 'update:visible': [value: boolean] }>();
</script>

<style scoped>
.citation-empty {
  padding: 24px;
  text-align: center;
  color: #909399;
}
.citation-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.citation-item {
  padding: 12px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafbfc;
}
.citation-item-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.citation-doc {
  flex: 1;
  font-weight: 500;
}
.citation-chunk-id {
  display: block;
  font-size: 11px;
  color: #909399;
  word-break: break-all;
}
</style>