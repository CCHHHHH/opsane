<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import OnboardingTour from './components/common/OnboardingTour.vue'
import ToastViewport from './components/common/ToastViewport.vue'
import { applyTheme, nextTheme, readStoredTheme } from './utils/theme'

const route = useRoute()
const theme = ref(readStoredTheme())
const onboardingTour = ref<InstanceType<typeof OnboardingTour> | null>(null)

applyTheme(theme.value)

const navItems = [
  { to: '/chat', label: '聊天', icon: '✦' },
  { to: '/terminal', label: '终端', icon: '›_' },
  { to: '/servers', label: '资源', icon: '◇' },
  { to: '/config', label: '配置', icon: '⚙' },
  { to: '/memories', label: '记忆', icon: '◎' },
  { to: '/audit', label: '审计', icon: '☷' },
]

const currentTitle = computed(() => String(route.meta.label ?? '工作台'))
const themeSwitchLabel = computed(() => theme.value === 'dark' ? '切换到浅色模式' : '切换到深色模式')

function toggleTheme() {
  theme.value = applyTheme(nextTheme(theme.value))
}

function openOnboarding() {
  void onboardingTour.value?.start()
}
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink class="brand" to="/chat" aria-label="Opsane 首页">
        <span class="brand-mark"><img :src="'/assets/logo.svg?v=opsane-2'" alt="" /></span>
        <span class="brand-copy">
          <strong>Opsane</strong>
          <small>智能运维工作台</small>
        </span>
      </RouterLink>

      <nav class="app-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="nav-link"
          :data-onboarding="item.to === '/servers' ? 'servers' : item.to === '/audit' ? 'audit' : undefined"
        >
          <span class="nav-icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <button class="onboarding-help" type="button" aria-label="打开新手引导" title="新手引导" @click="openOnboarding">?</button>

      <button
        class="theme-switch"
        type="button"
        role="switch"
        :aria-checked="theme === 'light'"
        :aria-label="themeSwitchLabel"
        :title="themeSwitchLabel"
        @click="toggleTheme"
      >
        <span class="theme-switch-track" aria-hidden="true">
          <span class="theme-switch-sun" />
          <span class="theme-switch-moon" />
          <span class="theme-switch-thumb" />
        </span>
      </button>
    </header>

    <main class="app-main" :aria-label="currentTitle">
      <RouterView />
    </main>
    <ToastViewport />
    <OnboardingTour ref="onboardingTour" />
  </div>
</template>
