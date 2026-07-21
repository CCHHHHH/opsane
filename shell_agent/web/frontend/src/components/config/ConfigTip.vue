<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId } from 'vue'
import type { CSSProperties } from 'vue'

defineProps<{
  text: string
}>()

const anchor = ref<HTMLElement | null>(null)
const popover = ref<HTMLElement | null>(null)
const open = ref(false)
const positioned = ref(false)
const hovered = ref(false)
const focused = ref(false)
const side = ref<'top' | 'bottom'>('top')
const popoverStyle = ref<CSSProperties>({ left: '0px', top: '0px' })
const tooltipId = `config-tip-${useId()}`
let listening = false

function positionPopover() {
  if (!anchor.value || !popover.value) return
  const view = anchor.value.ownerDocument.defaultView
  if (!view) return
  const margin = 12
  const gap = 8
  const anchorRect = anchor.value.getBoundingClientRect()
  const popoverRect = popover.value.getBoundingClientRect()
  const left = Math.min(
    Math.max(anchorRect.left + anchorRect.width / 2 - popoverRect.width / 2, margin),
    view.innerWidth - popoverRect.width - margin,
  )
  const preferredTop = anchorRect.top - popoverRect.height - gap
  const showBelow = preferredTop < margin
  const top = showBelow
    ? Math.min(anchorRect.bottom + gap, view.innerHeight - popoverRect.height - margin)
    : preferredTop

  side.value = showBelow ? 'bottom' : 'top'
  popoverStyle.value = {
    left: `${Math.max(margin, left)}px`,
    top: `${Math.max(margin, top)}px`,
  }
  positioned.value = true
}

function startListening() {
  if (listening) return
  const view = anchor.value?.ownerDocument.defaultView
  if (!view) return
  listening = true
  view.addEventListener('resize', positionPopover)
  view.addEventListener('scroll', positionPopover, true)
}

function stopListening() {
  if (!listening) return
  listening = false
  const view = anchor.value?.ownerDocument.defaultView
  view?.removeEventListener('resize', positionPopover)
  view?.removeEventListener('scroll', positionPopover, true)
}

async function syncPopover() {
  const shouldOpen = hovered.value || focused.value
  open.value = shouldOpen
  if (!shouldOpen) {
    positioned.value = false
    stopListening()
    return
  }

  startListening()
  await nextTick()
  positionPopover()
}

function handleMouseEnter() {
  hovered.value = true
  void syncPopover()
}

function handleMouseLeave() {
  hovered.value = false
  void syncPopover()
}

function handleFocus() {
  focused.value = true
  void syncPopover()
}

function handleBlur() {
  focused.value = false
  void syncPopover()
}

function closePopover() {
  hovered.value = false
  focused.value = false
  void syncPopover()
}

onBeforeUnmount(stopListening)
</script>

<template>
  <span
    ref="anchor"
    class="config-tip"
    tabindex="0"
    :aria-label="text"
    :aria-describedby="tooltipId"
    @mouseenter="handleMouseEnter"
    @mouseleave="handleMouseLeave"
    @focus="handleFocus"
    @blur="handleBlur"
    @keydown.esc="closePopover"
  >
    <span aria-hidden="true">i</span>
  </span>
  <Teleport to="body">
    <span
      v-show="open"
      :id="tooltipId"
      ref="popover"
      class="config-tip-popover"
      :class="{ positioned }"
      :data-side="side"
      :style="popoverStyle"
      role="tooltip"
    >{{ text }}</span>
  </Teleport>
</template>

<style scoped>
.config-tip {
  position: relative;
  width: 15px;
  height: 15px;
  display: inline-grid;
  flex: 0 0 15px;
  place-items: center;
  border: 1px solid var(--border-light);
  border-radius: 50%;
  color: var(--text-muted);
  font-family: Georgia, serif;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
}

.config-tip:hover,
.config-tip:focus-visible {
  border-color: var(--accent);
  outline: none;
  color: var(--text-primary);
}

.config-tip-popover {
  position: fixed;
  z-index: 1200;
  width: min(300px, calc(100vw - 24px));
  padding: 9px 11px;
  border: 1px solid var(--border-light);
  border-radius: 7px;
  background: var(--dialog-surface);
  box-shadow: var(--shadow);
  color: var(--text-secondary);
  font-family: inherit;
  font-size: 12px;
  font-weight: 400;
  line-height: 1.55;
  opacity: 0;
  pointer-events: none;
  text-align: left;
  transform: translateY(3px);
  transition: opacity 120ms ease, transform 120ms ease;
  visibility: hidden;
  white-space: normal;
  overflow-wrap: anywhere;
}

.config-tip-popover[data-side="bottom"] {
  transform: translateY(-3px);
}

.config-tip-popover.positioned {
  opacity: 1;
  transform: translateY(0);
  visibility: visible;
}
</style>
