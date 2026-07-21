import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const backendTarget = env.VITE_BACKEND_URL || 'http://127.0.0.1:8010'
  return {
    base: '/next/',
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    build: {
      outDir: '../static/next',
      emptyOutDir: true,
    },
    server: {
      port: 5173,
      proxy: {
        '/api': backendTarget,
        '/ws': {
          target: backendTarget,
          ws: true,
        },
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      include: ['src/**/*.spec.ts'],
    },
  }
})
