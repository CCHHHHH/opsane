<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import type { CSSProperties } from 'vue'
import { useRouter } from 'vue-router'

import { completeOnboarding, shouldShowOnboarding } from '../../utils/onboarding'

type TourPlacement = 'top' | 'bottom'

interface TourStep {
  target: string
  eyebrow: string
  title: string
  description: string
  placement: TourPlacement
}

const steps: TourStep[] = [
  {
    target: '[data-onboarding="config"]',
    eyebrow: '模型配置',
    title: '先连接 LLM',
    description: '在配置页填写 Provider、模型、API Key 和 Base URL，先测试连接，成功后保存。密钥由本机后端保存，页面不会回显原值。',
    placement: 'bottom',
  },
  {
    target: '[data-onboarding="servers"]',
    eyebrow: '准备资源',
    title: '先登记服务器',
    description: '在资源页维护服务器别名、地址和凭证。后续对话只需要引用别名，真实凭证不会交给模型。',
    placement: 'bottom',
  },
  {
    target: '[data-onboarding="composer"]',
    eyebrow: '描述目标',
    title: '直接说你想完成什么',
    description: '例如“检查 dev-01 最近 30 分钟的错误日志”。Opsane 会理解目标、收集证据，并在变更前给出方案。',
    placement: 'top',
  },
  {
    target: '[data-onboarding="confirm-mode"]',
    eyebrow: '控制权限',
    title: '选择执行边界',
    description: '首次建议使用“安全自动”：安全的只读步骤可自动运行，改变服务器状态的操作仍需你确认。',
    placement: 'top',
  },
  {
    target: '[data-onboarding="session-files"]',
    eyebrow: '会话文件',
    title: '把资料带进当前会话',
    description: '可上传日志、配置和安装包，随后直接说“把刚上传的包传到 dev-01:/tmp”。文件只跟随当前会话。',
    placement: 'top',
  },
  {
    target: '[data-onboarding="audit"]',
    eyebrow: '操作留痕',
    title: '每次执行都可以追溯',
    description: '命令、确认、结果和耗时都会进入审计。需要复查问题时，可以按会话、服务器和操作类型查找记录。',
    placement: 'bottom',
  },
]

const router = useRouter()
const active = ref(false)
const currentIndex = ref(0)
const popover = ref<HTMLElement | null>(null)
const spotlightVisible = ref(false)
const spotlightStyle = ref<CSSProperties>({})
const popoverStyle = ref<CSSProperties>({ top: '72px', left: '12px' })

const currentStep = computed(() => steps[currentIndex.value])
const isFirstStep = computed(() => currentIndex.value === 0)
const isLastStep = computed(() => currentIndex.value === steps.length - 1)
const progressLabel = computed(() => `${currentIndex.value + 1} / ${steps.length}`)

let autoStartTimer: ReturnType<typeof setTimeout> | null = null
let positionFrame = 0
let positionToken = 0
let previousFocus: HTMLElement | null = null

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

async function findTarget(selector: string): Promise<HTMLElement | null> {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const target = document.querySelector<HTMLElement>(selector)
    if (target) return target
    await delay(50)
  }
  return null
}

async function positionCurrentStep(): Promise<void> {
  if (!active.value) return
  const token = ++positionToken
  const step = currentStep.value
  const target = await findTarget(step.target)
  if (!active.value || token !== positionToken || step !== currentStep.value) return

  const viewport = document.documentElement
  const viewportWidth = viewport.clientWidth
  const viewportHeight = viewport.clientHeight
  const margin = 12
  const gap = 14

  if (!target) {
    spotlightVisible.value = false
    await nextTick()
    const panelRect = popover.value?.getBoundingClientRect()
    const panelWidth = panelRect?.width ?? Math.min(340, viewportWidth - margin * 2)
    const panelHeight = panelRect?.height ?? 220
    popoverStyle.value = {
      left: `${Math.max(margin, (viewportWidth - panelWidth) / 2)}px`,
      top: `${Math.max(margin, (viewportHeight - panelHeight) / 2)}px`,
    }
    return
  }

  target.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  await nextTick()
  if (!active.value || token !== positionToken) return

  const targetRect = target.getBoundingClientRect()
  const spotlightPadding = 6
  spotlightStyle.value = {
    top: `${Math.max(4, targetRect.top - spotlightPadding)}px`,
    left: `${Math.max(4, targetRect.left - spotlightPadding)}px`,
    width: `${Math.min(viewportWidth - 8, targetRect.width + spotlightPadding * 2)}px`,
    height: `${Math.min(viewportHeight - 8, targetRect.height + spotlightPadding * 2)}px`,
  }
  spotlightVisible.value = true

  await nextTick()
  const panelRect = popover.value?.getBoundingClientRect()
  const panelWidth = panelRect?.width ?? Math.min(340, viewportWidth - margin * 2)
  const panelHeight = panelRect?.height ?? 220
  const left = clamp(
    targetRect.left + targetRect.width / 2 - panelWidth / 2,
    margin,
    viewportWidth - panelWidth - margin,
  )
  const preferredTop = step.placement === 'top'
    ? targetRect.top - panelHeight - gap
    : targetRect.bottom + gap
  const alternateTop = step.placement === 'top'
    ? targetRect.bottom + gap
    : targetRect.top - panelHeight - gap
  const fitsPreferred = preferredTop >= margin && preferredTop + panelHeight <= viewportHeight - margin
  const top = clamp(
    fitsPreferred ? preferredTop : alternateTop,
    margin,
    viewportHeight - panelHeight - margin,
  )

  popoverStyle.value = { left: `${left}px`, top: `${top}px` }
}

function schedulePosition(): void {
  if (!active.value) return
  const view = document.defaultView
  if (!view) return
  if (positionFrame) view.cancelAnimationFrame(positionFrame)
  positionFrame = view.requestAnimationFrame(() => {
    positionFrame = 0
    void positionCurrentStep()
  })
}

async function focusPopover(): Promise<void> {
  await nextTick()
  popover.value?.focus({ preventScroll: true })
}

async function showStep(index: number): Promise<void> {
  currentIndex.value = clamp(index, 0, steps.length - 1)
  spotlightVisible.value = false
  await nextTick()
  await positionCurrentStep()
  await focusPopover()
}

async function start(): Promise<void> {
  if (!active.value) previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  active.value = true
  await router.push('/chat')
  await showStep(0)
}

async function closeTour(): Promise<void> {
  active.value = false
  spotlightVisible.value = false
  positionToken += 1
  await nextTick()
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
  previousFocus = null
}

async function finish(): Promise<void> {
  completeOnboarding()
  await closeTour()
}

async function nextStep(): Promise<void> {
  if (isLastStep.value) {
    await finish()
    return
  }
  await showStep(currentIndex.value + 1)
}

async function previousStep(): Promise<void> {
  if (!isFirstStep.value) await showStep(currentIndex.value - 1)
}

async function skip(): Promise<void> {
  completeOnboarding()
  await closeTour()
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault()
    void skip()
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    void nextStep()
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    void previousStep()
  }
}

onMounted(() => {
  const view = document.defaultView
  view?.addEventListener('resize', schedulePosition)
  document.addEventListener('scroll', schedulePosition, true)
  if (shouldShowOnboarding()) {
    autoStartTimer = setTimeout(() => void start(), 420)
  }
})

onBeforeUnmount(() => {
  const view = document.defaultView
  view?.removeEventListener('resize', schedulePosition)
  document.removeEventListener('scroll', schedulePosition, true)
  if (autoStartTimer) clearTimeout(autoStartTimer)
  if (positionFrame) view?.cancelAnimationFrame(positionFrame)
})

defineExpose({ start })
</script>

<template>
  <Teleport to="body">
    <Transition name="onboarding-fade">
      <div v-if="active" class="onboarding-tour-layer" @click.stop @keydown="handleKeydown">
        <div v-if="spotlightVisible" class="onboarding-spotlight" :style="spotlightStyle" aria-hidden="true" />

        <section
          ref="popover"
          class="onboarding-popover"
          :style="popoverStyle"
          role="dialog"
          aria-modal="true"
          :aria-label="currentStep.title"
          tabindex="-1"
          @click.stop
        >
          <header class="onboarding-heading">
            <div>
              <span class="onboarding-eyebrow">{{ currentStep.eyebrow }}</span>
              <h2>{{ currentStep.title }}</h2>
            </div>
            <button class="onboarding-close" type="button" aria-label="关闭新手引导" title="关闭新手引导" @click="skip">×</button>
          </header>

          <p>{{ currentStep.description }}</p>

          <div class="onboarding-progress" aria-label="引导进度">
            <span>{{ progressLabel }}</span>
            <span class="onboarding-progress-track" aria-hidden="true">
              <i
                v-for="(_, stepIndex) in steps"
                :key="stepIndex"
                :class="{ active: stepIndex <= currentIndex }"
              />
            </span>
          </div>

          <footer class="onboarding-actions">
            <button class="onboarding-button subtle" type="button" @click="skip">跳过</button>
            <button v-if="!isFirstStep" class="onboarding-button" type="button" @click="previousStep">上一步</button>
            <button class="onboarding-button primary" type="button" @click="nextStep">
              {{ isLastStep ? '开始使用' : '下一步' }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.onboarding-tour-layer {
  position: fixed;
  z-index: 1500;
  inset: 0;
  overflow: hidden;
}

.onboarding-spotlight {
  position: fixed;
  z-index: 1501;
  border: 2px solid var(--accent);
  border-radius: 8px;
  box-shadow: 0 0 0 9999px rgba(2, 8, 16, 0.74), 0 0 0 4px rgba(24, 184, 231, 0.18);
  pointer-events: none;
  transition: top 180ms ease, left 180ms ease, width 180ms ease, height 180ms ease;
}

.onboarding-popover {
  position: fixed;
  z-index: 1502;
  width: min(340px, calc(100vw - 24px));
  padding: 18px;
  border: 1px solid var(--border-light);
  border-radius: 8px;
  outline: none;
  background: var(--dialog-surface);
  box-shadow: var(--floating-shadow);
  color: var(--text-primary);
}

.onboarding-popover:focus-visible {
  box-shadow: var(--floating-shadow), 0 0 0 2px var(--accent-soft);
}

.onboarding-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.onboarding-heading h2 {
  margin: 4px 0 0;
  font-size: 18px;
  line-height: 1.3;
}

.onboarding-eyebrow {
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}

.onboarding-close {
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 20px;
  line-height: 1;
}

.onboarding-close:hover,
.onboarding-close:focus-visible {
  border-color: var(--border);
  background: var(--surface-hover);
  color: var(--text-primary);
}

.onboarding-popover p {
  margin: 14px 0 16px;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.65;
}

.onboarding-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-muted);
  font-size: 11px;
}

.onboarding-progress-track {
  display: flex;
  gap: 4px;
  flex: 1;
}

.onboarding-progress-track i {
  height: 3px;
  flex: 1;
  border-radius: 2px;
  background: var(--border);
}

.onboarding-progress-track i.active {
  background: var(--accent);
}

.onboarding-actions {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  margin-top: 18px;
}

.onboarding-button {
  min-height: 32px;
  padding: 6px 11px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font-size: 12px;
}

.onboarding-button:hover,
.onboarding-button:focus-visible {
  border-color: var(--border-light);
  background: var(--surface-hover);
}

.onboarding-button.subtle {
  border-color: transparent;
  background: transparent;
  color: var(--text-secondary);
}

.onboarding-button.primary {
  border-color: var(--accent);
  background: var(--accent);
  color: #03131c;
  font-weight: 700;
}

.onboarding-button.primary:hover,
.onboarding-button.primary:focus-visible {
  border-color: var(--accent-hover);
  background: var(--accent-hover);
}

.onboarding-fade-enter-active,
.onboarding-fade-leave-active {
  transition: opacity 140ms ease;
}

.onboarding-fade-enter-from,
.onboarding-fade-leave-to {
  opacity: 0;
}

@media (max-width: 520px) {
  .onboarding-popover {
    padding: 15px;
  }

  .onboarding-heading h2 {
    font-size: 16px;
  }

  .onboarding-actions {
    flex-wrap: wrap;
  }
}

@media (prefers-reduced-motion: reduce) {
  .onboarding-spotlight,
  .onboarding-fade-enter-active,
  .onboarding-fade-leave-active {
    transition: none;
  }
}
</style>
