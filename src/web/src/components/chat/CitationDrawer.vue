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
        <div class="citation-item-rule" aria-hidden="true"></div>
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
/* PlanA v1.0 visual pass — drawer + signature 3px accent rule column on every row. */
:deep(.el-drawer) {
  border-radius: var(--r-lg);
}
:deep(.el-drawer__header) {
  padding: var(--s-4) var(--s-5);
  font-family: var(--font-display);
  font-size: var(--fs-18);
  font-weight: 600;
  letter-spacing: var(--ls-display);
  border-bottom: var(--hairline);
  color: var(--text-1);
}
:deep(.el-drawer__body) {
  padding: var(--s-4) var(--s-5);
  background: var(--bg);
}
.citation-empty {
  padding: var(--s-6);
  text-align: center;
  color: var(--text-3);
  font-size: var(--fs-13);
}
.citation-list {
  display: flex;
  flex-direction: column;
  gap: var(--s-3);
}
.citation-item {
  display: grid;
  grid-template-columns: 4px 1fr auto;
  column-gap: var(--s-3);
  padding: var(--s-3) var(--s-4);
  background: var(--cite-bg);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
}
.citation-item-rule {
  grid-column: 1 / 2;
  grid-row: 1 / 3;
  background: var(--accent);
  border-radius: 2px;
}
.citation-item-header {
  grid-column: 2 / 3;
  display: flex;
  align-items: center;
  gap: var(--s-2);
  flex-wrap: wrap;
  min-width: 0;
  margin-bottom: var(--s-2);
  font-size: var(--fs-13);
  color: var(--text-1);
}
.citation-doc {
  flex: 1;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
:deep(.citation-item-header .el-tag) {
  border-radius: var(--r-sm);
  font-family: var(--font-mono);
  font-size: var(--fs-12);
}
.citation-chunk-id {
  grid-column: 2 / 3;
  display: block;
  font-family: var(--font-mono);
  font-size: var(--fs-12);
  color: var(--text-3);
  word-break: break-all;
}
</style>