import { createRouter, createWebHashHistory } from 'vue-router'

import AuditPage from './pages/AuditPage.vue'
import ChatPage from './pages/ChatPage.vue'
import ConfigPage from './pages/ConfigPage.vue'
import MemoriesPage from './pages/MemoriesPage.vue'
import ServersPage from './pages/ServersPage.vue'
import TerminalPage from './pages/TerminalPage.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/chat', name: 'chat', component: ChatPage, meta: { label: '聊天', icon: '✦' } },
    { path: '/terminal', name: 'terminal', component: TerminalPage, meta: { label: '终端', icon: '›_' } },
    { path: '/servers', name: 'servers', component: ServersPage, meta: { label: '资源', icon: '◇' } },
    { path: '/config', name: 'config', component: ConfigPage, meta: { label: '配置', icon: '⚙' } },
    { path: '/memories', name: 'memories', component: MemoriesPage, meta: { label: '记忆', icon: '◎' } },
    { path: '/audit', name: 'audit', component: AuditPage, meta: { label: '审计', icon: '☷' } },
  ],
})
