<script setup lang="ts">
defineProps<{
  page: number
  totalPages: number
  total: number
}>()

const emit = defineEmits<{
  change: [page: number]
}>()
</script>

<template>
  <nav v-if="total > 0" class="pagination-bar" aria-label="分页">
    <span class="pagination-total">共 {{ total }} 条</span>
    <div class="pagination-actions">
      <button class="btn btn-small" type="button" :disabled="page <= 1" @click="emit('change', page - 1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页</span>
      <button class="btn btn-small" type="button" :disabled="page >= totalPages" @click="emit('change', page + 1)">下一页</button>
    </div>
  </nav>
</template>

<style scoped>
.pagination-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; border-top: 1px solid var(--border); color: var(--text-secondary); }
.pagination-total { color: var(--text-muted); font-size: 11px; }
.pagination-actions { display: flex; align-items: center; gap: 10px; font-size: 11px; }
@media(max-width:520px){.pagination-bar{align-items:flex-start;flex-direction:column}.pagination-actions{width:100%;justify-content:space-between}}
</style>
