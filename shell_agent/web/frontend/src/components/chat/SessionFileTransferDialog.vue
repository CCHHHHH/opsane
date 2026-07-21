<script setup lang="ts">
import { computed, reactive, watch } from 'vue'

import type { ServerRecord } from '../../api/protocol'
import type {
  SessionFileRecord,
  SessionFileTransferInput,
  SessionFileTransferState,
} from '../../stores/sessionFiles'

const props = defineProps<{
  file: SessionFileRecord
  servers: ServerRecord[]
  state?: SessionFileTransferState
  serversLoading?: boolean
  serversError?: string
}>()

const emit = defineEmits<{
  close: []
  submit: [input: SessionFileTransferInput]
}>()

const form = reactive<SessionFileTransferInput>({
  target: '',
  remote_dir: '/tmp/shell-agent-uploads',
  remote_name: props.file.name,
  overwrite: false,
})
const attempted = reactive({ submit: false })

const submitting = computed(() => ['submitting', 'running'].includes(props.state?.status ?? ''))
const succeeded = computed(() => props.state?.status === 'success' && Boolean(props.state.result))
const failed = computed(() => props.state?.status === 'failed')
const selectedServer = computed(() => props.servers.find((server) => server.alias === form.target))
const productionTarget = computed(() => selectedServer.value?.env === 'prod')
const remoteDirError = computed(() => {
  const value = form.remote_dir.trim()
  if (!value) return '请输入远端目录'
  if (!value.startsWith('/')) return '远端目录必须是绝对路径'
  if (/\0|[\x01-\x1f\x7f]/.test(value)) return '远端目录包含不允许的控制字符'
  return ''
})
const remoteNameError = computed(() => {
  const value = form.remote_name.trim()
  if (!value) return '请输入远端文件名'
  if (value === '.' || value === '..' || /[/\\]/.test(value)) return '远端文件名不能包含路径分隔符'
  if (/\0|[\x01-\x1f\x7f]/.test(value)) return '远端文件名包含不允许的控制字符'
  return ''
})
const remotePath = computed(() => {
  const directory = form.remote_dir.trim().replace(/\/+$/, '') || '/'
  return directory === '/' ? `/${form.remote_name.trim()}` : `${directory}/${form.remote_name.trim()}`
})
const canSubmit = computed(() => Boolean(
  form.target
  && !remoteDirError.value
  && !remoteNameError.value
  && !submitting.value
  && !props.serversLoading,
))

watch(() => [props.file.id, props.state?.result?.id], () => {
  const result = props.state?.result
  form.target = result?.target || (props.servers.length === 1 ? props.servers[0].alias : '')
  form.remote_dir = result?.remote_dir || '/tmp/shell-agent-uploads'
  form.remote_name = result?.remote_name || props.file.name
  form.overwrite = Boolean(result?.overwrite)
  attempted.submit = false
}, { immediate: true })

watch(() => props.servers, (servers) => {
  if (!form.target && servers.length === 1) form.target = servers[0].alias
}, { immediate: true })

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

function close() {
  if (!submitting.value) emit('close')
}

function submit() {
  attempted.submit = true
  if (!canSubmit.value) return
  emit('submit', {
    target: form.target,
    remote_dir: form.remote_dir.trim(),
    remote_name: form.remote_name.trim(),
    overwrite: form.overwrite,
  })
}
</script>

<template>
  <Teleport to="body">
    <div class="transfer-backdrop" @click.self="close">
      <section
        class="transfer-dialog"
        role="dialog"
        aria-modal="true"
        :aria-busy="submitting || undefined"
        :aria-label="`将 ${file.name} 传到服务器`"
      >
        <header class="transfer-header">
          <div>
            <span class="transfer-eyebrow">会话文件传输</span>
            <h2>传到服务器</h2>
          </div>
          <button class="transfer-close" type="button" :disabled="submitting" aria-label="关闭" @click="close">×</button>
        </header>

        <div v-if="succeeded" class="transfer-result transfer-result-success" role="status">
          <span class="transfer-result-icon" aria-hidden="true">✓</span>
          <strong>文件传输完成</strong>
          <p>{{ state?.result?.target }}:{{ state?.result?.remote_path }}</p>
          <dl>
            <div><dt>大小</dt><dd>{{ formatBytes(state?.result?.size ?? file.size) }}</dd></div>
            <div><dt>SHA-256</dt><dd><code>{{ state?.result?.remote_sha256 || state?.result?.sha256 || file.sha256 }}</code></dd></div>
          </dl>
          <button class="btn btn-primary" type="button" @click="close">完成</button>
        </div>

        <form v-else class="transfer-form" @submit.prevent="submit">
          <div class="transfer-file">
            <span aria-hidden="true">▤</span>
            <div><strong>{{ file.name }}</strong><small>{{ formatBytes(file.size) }} · SHA-256 {{ file.sha256.slice(0, 12) }}…</small></div>
          </div>

          <div v-if="serversError" class="transfer-notice transfer-notice-error">{{ serversError }}</div>
          <div v-if="failed" class="transfer-notice transfer-notice-error" role="alert">
            {{ state?.error || state?.result?.error || '文件传输失败，请检查目标和远端路径后重试。' }}
          </div>
          <div v-if="productionTarget" class="transfer-notice transfer-notice-danger" role="alert">
            当前目标是生产环境，上传会直接写入服务器文件系统，请确认路径无误。
          </div>

          <label class="transfer-field">
            <span>目标服务器</span>
            <select v-model="form.target" class="select" required :disabled="submitting || serversLoading">
              <option disabled value="">{{ serversLoading ? '正在加载服务器…' : '请选择目标服务器' }}</option>
              <option v-for="server in servers" :key="server.alias" :value="server.alias">
                {{ server.alias }} · {{ server.env || 'dev' }}
              </option>
            </select>
            <small v-if="attempted.submit && !form.target" class="transfer-field-error">请选择目标服务器</small>
          </label>

          <label class="transfer-field">
            <span>远端目录</span>
            <input v-model="form.remote_dir" class="input mono" autocomplete="off" spellcheck="false" :disabled="submitting" />
            <small v-if="attempted.submit && remoteDirError" class="transfer-field-error">{{ remoteDirError }}</small>
          </label>

          <label class="transfer-field">
            <span>远端文件名</span>
            <input v-model="form.remote_name" class="input mono" autocomplete="off" spellcheck="false" :disabled="submitting" />
            <small v-if="attempted.submit && remoteNameError" class="transfer-field-error">{{ remoteNameError }}</small>
          </label>

          <div class="transfer-destination">
            <span>目标路径</span>
            <code>{{ form.target || '目标服务器' }}:{{ remotePath }}</code>
          </div>

          <label class="transfer-overwrite">
            <input v-model="form.overwrite" type="checkbox" :disabled="submitting" />
            <span>目标文件存在时允许覆盖</span>
          </label>

          <div class="transfer-notice">
            这是服务器写操作。Opsane 只传输文件，不会自动部署、解压或执行该文件。
          </div>

          <div v-if="submitting" class="transfer-progress" role="status">
            <span class="transfer-spinner" aria-hidden="true" />
            <span><strong>正在通过 SFTP 上传…</strong><small>大文件可能需要一些时间，请勿重复提交。</small></span>
          </div>

          <footer class="transfer-actions">
            <button class="btn" type="button" :disabled="submitting" @click="close">取消</button>
            <button class="btn btn-primary" type="submit" :disabled="!canSubmit">
              {{ submitting ? '上传中…' : failed ? '重试上传' : '确认并上传' }}
            </button>
          </footer>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.transfer-backdrop { position: fixed; z-index: 1100; inset: 0; display: grid; place-items: center; padding: 20px; background: rgba(1,7,15,.76); backdrop-filter: blur(5px); }
.transfer-dialog { width: min(520px,calc(100vw - 28px)); max-height: calc(100vh - 32px); overflow: auto; border: 1px solid rgba(24,184,231,.24); border-radius: 15px; background: var(--dialog-surface); box-shadow: var(--shadow); }
.transfer-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 16px 18px 13px; border-bottom: 1px solid var(--border); }
.transfer-header h2 { margin: 4px 0 0; font-size: 17px; }
.transfer-eyebrow { color: var(--text-muted); font-size: 10px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.transfer-close { width: 28px; height: 28px; padding: 0; border: 0; border-radius: 7px; background: transparent; color: var(--text-muted); font-size: 20px; }
.transfer-close:hover:not(:disabled) { background: var(--surface-hover); color: var(--text-primary); }
.transfer-form { display: grid; gap: 13px; padding: 16px 18px 18px; }
.transfer-file { min-width: 0; display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface-soft); }
.transfer-file > span { width: 34px; height: 34px; flex: 0 0 34px; display: grid; place-items: center; border-radius: 7px; background: var(--surface-hover); color: var(--text-secondary); }
.transfer-file > div { min-width: 0; display: grid; gap: 4px; }
.transfer-file strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.transfer-file small { color: var(--text-muted); font-size: 10px; }
.transfer-field { display: grid; gap: 6px; }
.transfer-field > span { color: var(--text-secondary); font-size: 12px; font-weight: 550; }
.transfer-field-error { color: var(--danger); font-size: 10px; }
.transfer-destination { min-width: 0; display: grid; gap: 5px; padding: 9px 10px; border-radius: 8px; background: var(--bg-primary); }
.transfer-destination span { color: var(--text-muted); font-size: 10px; }
.transfer-destination code { overflow-wrap: anywhere; color: var(--code-accent); font-size: 11px; }
.transfer-overwrite { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: 12px; }
.transfer-notice { padding: 9px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-soft); color: var(--text-muted); font-size: 11px; line-height: 1.5; }
.transfer-notice-error { border-color: rgba(255,112,111,.38); background: var(--danger-soft); color: var(--danger-text); }
.transfer-notice-danger { border-color: rgba(255,112,111,.48); background: var(--danger-soft); color: var(--danger-text); }
.transfer-progress { display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid rgba(24,184,231,.28); border-radius: 8px; background: var(--accent-soft); }
.transfer-progress > span:last-child { display: grid; gap: 3px; }
.transfer-progress strong { color: var(--code-accent); font-size: 12px; }
.transfer-progress small { color: var(--text-muted); font-size: 10px; }
.transfer-spinner { width: 17px; height: 17px; flex: 0 0 17px; border: 2px solid rgba(24,184,231,.25); border-top-color: var(--accent); border-radius: 50%; animation: transfer-spin .8s linear infinite; }
@keyframes transfer-spin { to { transform: rotate(360deg); } }
.transfer-actions { display: flex; justify-content: flex-end; gap: 8px; padding-top: 2px; }
.transfer-result { display: grid; justify-items: center; gap: 10px; padding: 28px 22px 22px; text-align: center; }
.transfer-result-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 50%; background: var(--success-soft); color: var(--success); font-size: 22px; }
.transfer-result p { max-width: 100%; margin: 0; overflow-wrap: anywhere; color: var(--text-secondary); font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: 12px; }
.transfer-result dl { width: 100%; display: grid; gap: 8px; margin: 4px 0 6px; text-align: left; }
.transfer-result dl > div { display: grid; grid-template-columns: 70px minmax(0,1fr); gap: 10px; padding: 8px 9px; border-radius: 7px; background: var(--bg-primary); }
.transfer-result dt { color: var(--text-muted); font-size: 11px; }
.transfer-result dd { min-width: 0; margin: 0; color: var(--text-secondary); font-size: 11px; overflow-wrap: anywhere; }
@media (max-width: 560px) { .transfer-backdrop { padding: 10px; } .transfer-header,.transfer-form { padding-right: 14px; padding-left: 14px; } }
</style>
