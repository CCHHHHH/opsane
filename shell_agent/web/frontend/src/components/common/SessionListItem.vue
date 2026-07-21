<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import type { SessionSummary } from '../../api/protocol'

const props = defineProps<{
  session: SessionSummary
  active?: boolean
}>()

const emit = defineEmits<{
  select: []
  rename: [title: string]
  pin: [pinned: boolean]
  remove: []
}>()

const row = ref<HTMLElement | null>(null)
const titleInput = ref<HTMLInputElement | null>(null)
const menuOpen = ref(false)
const editing = ref(false)
const title = ref('')
const pinned = computed(() => Boolean(props.session.pinned_at))

function openMenu() {
  menuOpen.value = !menuOpen.value
}

async function startRename() {
  menuOpen.value = false
  title.value = props.session.title || ''
  editing.value = true
  await nextTick()
  titleInput.value?.focus()
  titleInput.value?.select()
}

function finishRename() {
  if (!editing.value) return
  const nextTitle = title.value.trim()
  editing.value = false
  if (nextTitle && nextTitle !== props.session.title) emit('rename', nextTitle)
}

function cancelRename() {
  editing.value = false
}

function togglePin() {
  menuOpen.value = false
  emit('pin', !pinned.value)
}

function remove() {
  menuOpen.value = false
  emit('remove')
}

function handleDocumentPointerDown(event: PointerEvent) {
  if (row.value?.contains(event.target as Node)) return
  menuOpen.value = false
  if (editing.value) finishRename()
}

onMounted(() => document.addEventListener('pointerdown', handleDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', handleDocumentPointerDown))
</script>

<template>
  <div ref="row" class="session-list-item" :class="{ active, pinned }">
    <form v-if="editing" class="session-rename" @submit.prevent="finishRename">
      <input ref="titleInput" v-model="title" class="input" aria-label="会话名称" maxlength="120" @keydown.esc.prevent="cancelRename" />
      <button type="submit" aria-label="确认重命名" title="确认">✓</button>
    </form>
    <button v-else class="session-row-main" type="button" @click="emit('select')">
      <span v-if="pinned" class="session-pin-mark" title="已置顶" aria-label="已置顶">↑</span>
      <span class="session-row-copy">
        <strong>{{ session.title || '未命名会话' }}</strong>
        <small>{{ session.updated_at || session.last_message_at || '' }}</small>
      </span>
    </button>
    <button v-if="!editing" class="session-menu-trigger" type="button" aria-label="会话操作" title="会话操作" @click.stop="openMenu">⋯</button>
    <div v-if="menuOpen" class="session-menu" role="menu">
      <button type="button" role="menuitem" @click="startRename">重命名</button>
      <button type="button" role="menuitem" @click="togglePin">{{ pinned ? '取消置顶' : '置顶' }}</button>
      <button class="danger" type="button" role="menuitem" @click="remove">删除</button>
    </div>
  </div>
</template>

<style scoped>
.session-list-item { position: relative; width: 100%; min-height: 48px; display: flex; align-items: center; gap: 3px; margin-bottom: 4px; border: 1px solid transparent; border-radius: 7px; color: var(--text-secondary); }
.session-list-item:hover,.session-list-item.active { background: var(--bg-tertiary); color: var(--text-primary); }
.session-list-item.active { border-color: var(--border-light); }
.session-row-main { min-width: 0; min-height: 46px; display: flex; align-items: center; gap: 7px; flex: 1; padding: 8px; border: 0; background: transparent; color: inherit; text-align: left; }
.session-row-copy { min-width: 0; display: grid; gap: 4px; flex: 1; }
.session-row-copy strong,.session-row-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-row-copy strong { font-size: 12px; font-weight: 500; }
.session-row-copy small { color: var(--text-muted); font-size: 10px; }
.session-pin-mark { color: var(--accent); font-size: 11px; }
.session-menu-trigger { width: 28px; height: 28px; display: grid; place-items: center; flex: 0 0 auto; margin-right: 4px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--text-muted); font-size: 17px; opacity: 0; }
.session-list-item:hover .session-menu-trigger,.session-menu-trigger:focus-visible,.session-list-item.active .session-menu-trigger { opacity: 1; }
.session-menu-trigger:hover { background: var(--surface-hover); color: var(--text-primary); }
.session-menu { position: absolute; z-index: 30; top: 38px; right: 5px; min-width: 116px; overflow: hidden; padding: 4px; border: 1px solid var(--border-light); border-radius: 7px; background: var(--bg-elevated); box-shadow: var(--shadow); }
.session-menu button { width: 100%; display: block; padding: 7px 9px; border: 0; border-radius: 5px; background: transparent; color: var(--text-secondary); font-size: 11px; text-align: left; }
.session-menu button:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.session-menu button.danger { color: var(--danger); }
.session-rename { min-width: 0; display: flex; align-items: center; gap: 4px; flex: 1; padding: 6px; }
.session-rename .input { min-width: 0; min-height: 32px; height: 32px; padding: 5px 7px; font-size: 11px; }
.session-rename button { width: 28px; height: 28px; flex: 0 0 auto; padding: 0; border: 0; border-radius: 5px; background: var(--accent-soft); color: var(--accent); }
</style>
