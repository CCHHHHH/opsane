<script setup lang="ts">
defineProps<{
  loading?: boolean
  error?: string
  empty?: boolean
  emptyTitle?: string
  emptyDescription?: string
}>()

const emit = defineEmits<{
  retry: []
}>()
</script>

<template>
  <div v-if="loading" class="loading-state">
    <span class="spinner" aria-hidden="true" />
    <span>正在加载…</span>
  </div>
  <div v-else-if="error" class="empty-state">
    <strong>加载失败</strong>
    <span>{{ error }}</span>
    <button class="btn btn-small" type="button" @click="emit('retry')">重试</button>
  </div>
  <div v-else-if="empty" class="empty-state">
    <strong>{{ emptyTitle ?? '暂无数据' }}</strong>
    <span v-if="emptyDescription">{{ emptyDescription }}</span>
    <slot name="empty-actions" />
  </div>
  <slot v-else />
</template>
