<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { errorMessage } from '../api/http'
import type { CredentialRecord, ServerRecord, ServiceRecord } from '../api/protocol'
import AsyncState from '../components/common/AsyncState.vue'
import PageHeader from '../components/common/PageHeader.vue'
import { useInventoryStore, type CredentialInput, type ServerInput, type ServiceInput } from '../stores/inventory'
import { confirmAction } from '../utils/confirm'

type ResourceTab = 'servers' | 'services' | 'credentials'
type ServerConnectionState = 'idle' | 'testing' | 'success' | 'error'

const inventory = useInventoryStore()
const activeTab = ref<ResourceTab>('servers')
const showServerForm = ref(false)
const showCredentialForm = ref(false)
const showServiceForm = ref(false)
const originalAlias = ref('')
const originalServiceId = ref('')
const editingCredential = ref(false)
const serverTags = ref('')
const originalServerConnectionFingerprint = ref('')
const testedServerConnectionFingerprint = ref('')
const serverConnectionState = ref<ServerConnectionState>('idle')
const serverConnectionMessage = ref('保存前需要验证 SSH 地址、端口和凭证。')
const serviceOwners = ref('')
const servicePorts = ref('')
const serviceTags = ref('')
const serviceConfigPaths = ref('')
const localError = ref('')

const serverForm = reactive<ServerInput>({
  alias: '', host: '', port: 22, env: 'dev', role: '', ssh_credential: '', tags: [],
})
const credentialForm = reactive<CredentialInput>({
  id: '', type: 'password', username: '', password: '', private_key: '', passphrase: '',
})
const serviceForm = reactive<ServiceInput>({
  id: '', name: '', env: 'dev', owners: [], servers: [], deploy_dir: '', artifact_path: '', backup_dir: '', artifact_type: 'jar', startup_timeout_seconds: 60, log_dir: '', health_url: '', ports: [], start_cmd: '', stop_cmd: '', restart_cmd: '', status_cmd: '', config_paths: [], runtime: '', version: '', last_verified_at: '', verification_status: 'unknown', source_task_id: '', revision: 1, tags: [], notes: '',
})

function normalizedServerInput(): ServerInput {
  return {
    ...serverForm,
    alias: serverForm.alias.trim(),
    host: serverForm.host.trim(),
    port: Number(serverForm.port),
    role: serverForm.role.trim(),
    ssh_credential: serverForm.ssh_credential.trim(),
    tags: serverTags.value.split(',').map((tag) => tag.trim()).filter(Boolean),
  }
}

function serverConnectionFingerprint(input = normalizedServerInput()): string {
  return JSON.stringify({
    host: input.host,
    port: input.port,
    ssh_credential: input.ssh_credential,
  })
}

const serverConnectionReady = computed(() => {
  const input = normalizedServerInput()
  return Boolean(
    input.alias
    && input.host
    && input.ssh_credential
    && Number.isInteger(input.port)
    && input.port >= 1
    && input.port <= 65535
  )
})
const displayedServerConnectionState = computed<ServerConnectionState>(() => {
  if (
    ['success', 'error'].includes(serverConnectionState.value)
    && testedServerConnectionFingerprint.value !== serverConnectionFingerprint()
  ) return 'idle'
  return serverConnectionState.value
})
const displayedServerConnectionMessage = computed(() => {
  if (displayedServerConnectionState.value === 'idle' && testedServerConnectionFingerprint.value) {
    return '连接参数已变更，请重新测试。'
  }
  return serverConnectionMessage.value
})
const serverConnectionVerified = computed(() => (
  displayedServerConnectionState.value === 'success'
  && testedServerConnectionFingerprint.value === serverConnectionFingerprint()
))
const serverSaveLabel = computed(() => (
  !originalAlias.value
  && !serverConnectionVerified.value
    ? '测试并保存'
    : '保存'
))

function openServer(record?: ServerRecord) {
  localError.value = ''
  originalAlias.value = record?.alias ?? ''
  Object.assign(serverForm, {
    alias: record?.alias ?? '',
    host: record?.host ?? '',
    port: Number(record?.port ?? 22),
    env: record?.env ?? 'dev',
    role: record?.role ?? '',
    ssh_credential: record?.ssh_credential ?? inventory.credentials[0]?.id ?? '',
    tags: record?.tags ?? [],
  })
  serverTags.value = (record?.tags ?? []).join(', ')
  originalServerConnectionFingerprint.value = record ? serverConnectionFingerprint() : ''
  testedServerConnectionFingerprint.value = originalServerConnectionFingerprint.value
  serverConnectionState.value = 'idle'
  serverConnectionMessage.value = record
    ? '仅在修改主机、端口或凭证后需要重新测试。'
    : '保存前需要验证 SSH 地址、端口和凭证。'
  showServerForm.value = true
}

async function testServerConnection(input = normalizedServerInput()): Promise<boolean> {
  localError.value = ''
  if (!serverConnectionReady.value) {
    testedServerConnectionFingerprint.value = serverConnectionFingerprint(input)
    serverConnectionState.value = 'error'
    serverConnectionMessage.value = '请先完整填写别名、主机、端口和 SSH 凭证。'
    return false
  }
  const fingerprint = serverConnectionFingerprint(input)
  testedServerConnectionFingerprint.value = fingerprint
  serverConnectionState.value = 'testing'
  serverConnectionMessage.value = '正在建立 SSH 连接并执行只读探针…'
  try {
    const result = await inventory.testServerConnection(input)
    serverConnectionState.value = 'success'
    serverConnectionMessage.value = result.message || 'SSH 连接成功。'
    return true
  } catch (error) {
    serverConnectionState.value = 'error'
    serverConnectionMessage.value = errorMessage(error)
    return false
  }
}

async function saveServer() {
  localError.value = ''
  const input = normalizedServerInput()
  try {
    const connectionChanged = serverConnectionFingerprint(input)
      !== originalServerConnectionFingerprint.value
    if ((!originalAlias.value || connectionChanged) && !serverConnectionVerified.value) {
      if (!await testServerConnection(input)) return
    }
    await inventory.saveServer(input, originalAlias.value)
    showServerForm.value = false
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function removeServer(alias: string) {
  if (!confirmAction(`确定删除服务器 ${alias} 吗？`)) return
  try {
    await inventory.removeServer(alias)
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

function openService(record?: ServiceRecord) {
  localError.value = ''
  originalServiceId.value = record?.id ?? ''
  Object.assign(serviceForm, {
    id: record?.id ?? '',
    name: record?.name ?? '',
    env: record?.env ?? 'dev',
    owners: [...(record?.owners ?? [])],
    servers: [...(record?.servers ?? [])],
    deploy_dir: record?.deploy_dir ?? '',
    artifact_path: record?.artifact_path ?? '',
    backup_dir: record?.backup_dir ?? '',
    artifact_type: record?.artifact_type ?? 'jar',
    startup_timeout_seconds: Number(record?.startup_timeout_seconds ?? 60),
    log_dir: record?.log_dir ?? '',
    health_url: record?.health_url ?? '',
    ports: [...(record?.ports ?? [])],
    start_cmd: record?.start_cmd ?? '',
    stop_cmd: record?.stop_cmd ?? '',
    restart_cmd: record?.restart_cmd ?? '',
    status_cmd: record?.status_cmd ?? '',
    config_paths: [...(record?.config_paths ?? [])],
    runtime: record?.runtime ?? '',
    version: record?.version ?? '',
    last_verified_at: record?.last_verified_at ?? '',
    verification_status: record?.verification_status ?? 'unknown',
    source_task_id: record?.source_task_id ?? '',
    revision: Number(record?.revision ?? 1),
    tags: [...(record?.tags ?? [])],
    notes: record?.notes ?? '',
  })
  serviceOwners.value = (record?.owners ?? []).join(', ')
  servicePorts.value = (record?.ports ?? []).join(', ')
  serviceTags.value = (record?.tags ?? []).join(', ')
  serviceConfigPaths.value = (record?.config_paths ?? []).join(', ')
  showServiceForm.value = true
}

async function saveService() {
  localError.value = ''
  try {
    await inventory.saveService({
      ...serviceForm,
      id: serviceForm.id.trim(),
      name: serviceForm.name.trim(),
      owners: serviceOwners.value.split(',').map((item) => item.trim()).filter(Boolean),
      ports: servicePorts.value.split(',').map((item) => Number(item.trim())).filter((port) => Number.isInteger(port) && port > 0),
      config_paths: serviceConfigPaths.value.split(',').map((item) => item.trim()).filter(Boolean),
      tags: serviceTags.value.split(',').map((item) => item.trim()).filter(Boolean),
    }, originalServiceId.value)
    showServiceForm.value = false
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

function verificationLabel(status?: string): string {
  return ({ verified: '已验证', stale: '待复核', conflicted: '有冲突', unknown: '未验证' } as Record<string, string>)[status ?? ''] ?? status ?? '未验证'
}

async function removeService(id: string) {
  if (!confirmAction(`确定删除服务画像 ${id} 吗？`)) return
  try {
    await inventory.removeService(id)
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

function openCredential(record?: CredentialRecord) {
  localError.value = ''
  editingCredential.value = Boolean(record)
  Object.assign(credentialForm, {
    id: record?.id ?? '',
    type: record?.type === 'key' ? 'key' : 'password',
    username: record?.username ?? '',
    password: '', private_key: '', passphrase: '',
  })
  showCredentialForm.value = true
}

async function saveCredential() {
  localError.value = ''
  try {
    await inventory.saveCredential({ ...credentialForm })
    showCredentialForm.value = false
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function removeCredential(id: string) {
  if (!confirmAction(`确定删除凭证 ${id} 吗？`)) return
  try {
    await inventory.removeCredential(id)
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

onMounted(async () => {
  await inventory.load()
})
</script>

<template>
  <section class="page-shell">
    <div class="page-container">
      <PageHeader title="资源管理" description="统一维护 SSH 服务器、服务画像和凭证引用。">
        <template #actions>
          <button class="btn" type="button" :disabled="inventory.loading" @click="inventory.load()">刷新</button>
          <button v-if="activeTab === 'servers'" class="btn btn-primary" type="button" @click="openServer()">新增服务器</button>
          <button v-if="activeTab === 'services'" class="btn btn-primary" type="button" @click="openService()">新增服务</button>
          <button v-if="activeTab === 'credentials'" class="btn btn-primary" type="button" @click="openCredential()">新增凭证</button>
        </template>
      </PageHeader>

      <div v-if="localError || inventory.error" class="notice notice-error">{{ localError || inventory.error }}</div>

      <div class="resource-tabs">
        <button type="button" :class="{ active: activeTab === 'servers' }" @click="activeTab = 'servers'">服务器 <span>{{ inventory.servers.length }}</span></button>
        <button type="button" :class="{ active: activeTab === 'services' }" @click="activeTab = 'services'">服务画像 <span>{{ inventory.services.length }}</span></button>
        <button type="button" :class="{ active: activeTab === 'credentials' }" @click="activeTab = 'credentials'">SSH 凭证 <span>{{ inventory.credentials.length }}</span></button>
      </div>

      <AsyncState :loading="inventory.loading" :error="inventory.error" @retry="inventory.load()">
        <div v-if="activeTab === 'servers'" class="grid-cards server-grid">
          <article v-for="server in inventory.servers" :key="server.alias" class="panel-card server-card">
            <div class="server-card-top">
              <div>
                <div class="server-title"><span class="status-dot online" />{{ server.alias }}</div>
                <code>{{ server.host }}:{{ server.port ?? 22 }}</code>
              </div>
              <span class="badge" :class="server.env === 'prod' ? 'badge-danger' : server.env === 'test' ? 'badge-warning' : 'badge-success'">{{ server.env || 'dev' }}</span>
            </div>
            <dl class="resource-meta">
              <div><dt>角色</dt><dd>{{ server.role || '-' }}</dd></div>
              <div><dt>凭证</dt><dd>{{ server.ssh_credential || '-' }}</dd></div>
              <div><dt>用户</dt><dd>{{ server.ssh_username || '-' }}</dd></div>
              <div><dt>标签</dt><dd>{{ (server.tags ?? []).join('、') || '-' }}</dd></div>
            </dl>
            <div class="card-actions">
              <button class="btn btn-small" type="button" @click="openServer(server)">编辑</button>
              <button class="btn btn-danger btn-small" type="button" @click="removeServer(server.alias)">删除</button>
            </div>
          </article>
          <div v-if="!inventory.servers.length" class="panel-card empty-state resource-empty"><strong>暂无服务器</strong><span>先创建 SSH 凭证，再添加服务器。</span></div>
        </div>

        <div v-else-if="activeTab === 'services'" class="panel-card">
          <div v-if="!inventory.services.length" class="empty-state"><strong>暂无服务画像</strong><span>创建服务画像后可复用部署目录、日志目录和运维命令。</span></div>
          <div v-else class="data-table-wrap">
            <table class="data-table">
              <thead><tr><th>服务</th><th>环境</th><th>服务器</th><th>运行方式</th><th>版本</th><th>验证状态</th><th>部署目录</th><th>端口</th><th /></tr></thead>
              <tbody>
                <tr v-for="service in inventory.services" :key="service.id">
                  <td><strong>{{ service.name }}</strong><br /><code class="text-muted">{{ service.id }}</code></td>
                  <td><span class="badge">{{ service.env || 'dev' }}</span></td>
                  <td>{{ (service.servers ?? []).join('、') || '-' }}</td>
                  <td>{{ service.runtime || '-' }}</td>
                  <td>{{ service.version || '-' }}</td>
                  <td><span class="badge" :class="service.verification_status === 'conflicted' ? 'badge-danger' : service.verification_status === 'stale' ? 'badge-warning' : service.verification_status === 'verified' ? 'badge-success' : ''">{{ verificationLabel(service.verification_status) }}</span><small class="verification-time">{{ service.last_verified_at || '-' }}</small></td>
                  <td><code>{{ service.deploy_dir || '-' }}</code></td>
                  <td>{{ (service.ports ?? []).join(', ') || '-' }}</td>
                  <td><div class="inline-actions"><button class="btn btn-small" type="button" @click="openService(service)">编辑</button><button class="btn btn-danger btn-small" type="button" @click="removeService(service.id)">删除</button></div></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-else class="panel-card">
          <div v-if="!inventory.credentials.length" class="empty-state"><strong>暂无 SSH 凭证</strong><span>凭证密文只写入后端配置，不会在此回显。</span></div>
          <div v-else class="data-table-wrap">
            <table class="data-table">
              <thead><tr><th>ID</th><th>类型</th><th>用户名</th><th>密钥状态</th><th /></tr></thead>
              <tbody>
                <tr v-for="credential in inventory.credentials" :key="credential.id">
                  <td><code>{{ credential.id }}</code></td>
                  <td><span class="badge badge-accent">{{ credential.type || 'password' }}</span></td>
                  <td>{{ credential.username || '-' }}</td>
                  <td>{{ credential.password_set || credential.private_key_set ? '已配置' : '未配置' }}</td>
                  <td><div class="inline-actions"><button class="btn btn-small" type="button" @click="openCredential(credential)">编辑</button><button class="btn btn-danger btn-small" type="button" @click="removeCredential(credential.id)">删除</button></div></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </AsyncState>
    </div>

    <div v-if="showServiceForm" class="dialog-backdrop" @click.self="showServiceForm = false">
      <form class="dialog service-dialog" @submit.prevent="saveService">
        <div class="dialog-header"><h2>{{ originalServiceId ? '编辑服务画像' : '新增服务画像' }}</h2><button class="btn btn-ghost btn-small" type="button" @click="showServiceForm = false">关闭</button></div>
        <div class="dialog-body form-grid">
          <div class="field"><label for="service-id">服务标识</label><input id="service-id" v-model="serviceForm.id" class="input" placeholder="留空时由名称生成" /></div>
          <div class="field"><label for="service-name">服务名称</label><input id="service-name" v-model="serviceForm.name" class="input" required /></div>
          <div class="field"><label for="service-env">环境</label><input id="service-env" v-model="serviceForm.env" class="input" /></div>
          <div class="field"><label for="service-owners">负责人</label><input id="service-owners" v-model="serviceOwners" class="input" placeholder="逗号分隔" /></div>
          <div class="field span-2"><span class="field-label">绑定服务器</span><div class="server-checkboxes"><label v-for="server in inventory.servers" :key="server.alias"><input v-model="serviceForm.servers" type="checkbox" :value="server.alias" />{{ server.alias }}</label><span v-if="!inventory.servers.length" class="text-muted">暂无服务器</span></div></div>
          <div class="field"><label for="service-deploy">部署目录</label><input id="service-deploy" v-model="serviceForm.deploy_dir" class="input mono" /></div>
          <div class="field"><label for="service-artifact-type">制品类型</label><select id="service-artifact-type" v-model="serviceForm.artifact_type" class="select"><option value="jar">Java JAR</option><option value="war">Tomcat WAR</option></select></div>
          <div class="field"><label for="service-artifact-path">当前制品路径</label><input id="service-artifact-path" v-model="serviceForm.artifact_path" class="input mono" :placeholder="serviceForm.artifact_type === 'war' ? '/opt/tomcat/webapps/platform.war' : '/data/app/service/lib/service.jar'" /></div>
          <div class="field"><label for="service-backup-dir">部署备份目录</label><input id="service-backup-dir" v-model="serviceForm.backup_dir" class="input mono" placeholder="/data/backup/service" /></div>
          <div class="field"><label for="service-startup-timeout">启动检查超时（秒）</label><input id="service-startup-timeout" v-model.number="serviceForm.startup_timeout_seconds" class="input" type="number" min="1" max="900" /></div>
          <div class="field"><label for="service-log">日志目录</label><input id="service-log" v-model="serviceForm.log_dir" class="input mono" /></div>
          <div class="field"><label for="service-health">健康检查 URL</label><input id="service-health" v-model="serviceForm.health_url" class="input" /></div>
          <div class="field"><label for="service-ports">端口</label><input id="service-ports" v-model="servicePorts" class="input" placeholder="8080, 9090" /></div>
          <div class="field"><label for="service-runtime">运行方式</label><select id="service-runtime" v-model="serviceForm.runtime" class="select"><option value="">未指定</option><option value="systemd">systemd</option><option value="docker">docker</option><option value="tomcat">tomcat</option><option value="standalone">standalone</option></select></div>
          <div class="field"><label for="service-version">版本</label><input id="service-version" v-model="serviceForm.version" class="input" /></div>
          <div class="field span-2"><label for="service-config-paths">配置文件</label><input id="service-config-paths" v-model="serviceConfigPaths" class="input mono" placeholder="逗号分隔，例如 /etc/my.cnf, /etc/my.cnf.d/server.cnf" /></div>
          <div class="field"><label for="service-start">启动命令</label><input id="service-start" v-model="serviceForm.start_cmd" class="input mono" /></div>
          <div class="field"><label for="service-stop">停止命令</label><input id="service-stop" v-model="serviceForm.stop_cmd" class="input mono" /></div>
          <div class="field"><label for="service-restart">重启命令</label><input id="service-restart" v-model="serviceForm.restart_cmd" class="input mono" /></div>
          <div class="field"><label for="service-status">状态命令</label><input id="service-status" v-model="serviceForm.status_cmd" class="input mono" /></div>
          <div class="field"><label for="service-verification">验证状态</label><select id="service-verification" v-model="serviceForm.verification_status" class="select"><option value="unknown">未验证</option><option value="verified">已验证</option><option value="stale">待复核</option><option value="conflicted">有冲突</option></select></div>
          <div class="field"><span class="field-label">画像元数据</span><div class="profile-metadata">revision {{ serviceForm.revision }}<span>{{ serviceForm.last_verified_at || '尚未验证' }}</span><code v-if="serviceForm.source_task_id">{{ serviceForm.source_task_id }}</code></div></div>
          <div class="field span-2"><label for="service-tags">标签</label><input id="service-tags" v-model="serviceTags" class="input" placeholder="逗号分隔" /></div>
          <div class="field span-2"><label for="service-notes">备注</label><textarea id="service-notes" v-model="serviceForm.notes" class="textarea" /></div>
        </div>
        <div class="dialog-footer"><button class="btn" type="button" @click="showServiceForm = false">取消</button><button class="btn btn-primary" type="submit" :disabled="inventory.saving">保存服务</button></div>
      </form>
    </div>

    <div v-if="showServerForm" class="dialog-backdrop" @click.self="showServerForm = false">
      <form class="dialog" @submit.prevent="saveServer">
        <div class="dialog-header"><h2>{{ originalAlias ? '编辑服务器' : '新增服务器' }}</h2><button class="btn btn-ghost btn-small" type="button" @click="showServerForm = false">关闭</button></div>
        <div class="dialog-body form-grid">
          <div class="field"><label for="server-alias">别名</label><input id="server-alias" v-model="serverForm.alias" class="input" required /></div>
          <div class="field"><label for="server-env">环境</label><select id="server-env" v-model="serverForm.env" class="select"><option value="dev">dev</option><option value="test">test</option><option value="prod">prod</option></select></div>
          <div class="field"><label for="server-host">主机</label><input id="server-host" v-model="serverForm.host" class="input" required /></div>
          <div class="field"><label for="server-port">端口</label><input id="server-port" v-model.number="serverForm.port" class="input" type="number" min="1" max="65535" required /></div>
          <div class="field"><label for="server-role">角色</label><input id="server-role" v-model="serverForm.role" class="input" placeholder="web / db / cache" /></div>
          <div class="field"><label for="server-credential">SSH 凭证</label><select id="server-credential" v-model="serverForm.ssh_credential" class="select" required><option disabled value="">请选择</option><option v-for="credential in inventory.credentials" :key="credential.id" :value="credential.id">{{ credential.id }} · {{ credential.username }}</option></select></div>
          <div class="field span-2"><label for="server-tags">标签</label><input id="server-tags" v-model="serverTags" class="input" placeholder="逗号分隔" /></div>
          <div class="server-connection-check span-2" :data-state="displayedServerConnectionState">
            <div>
              <strong>SSH 连通性</strong>
              <small>{{ displayedServerConnectionMessage }}</small>
            </div>
            <button class="btn btn-small" type="button" :disabled="inventory.testingServer || inventory.saving || !serverConnectionReady" @click="testServerConnection()">
              {{ inventory.testingServer ? '测试中…' : '测试连接' }}
            </button>
          </div>
        </div>
        <div class="dialog-footer"><button class="btn" type="button" @click="showServerForm = false">取消</button><button class="btn btn-primary" type="submit" :disabled="inventory.saving || inventory.testingServer">{{ serverSaveLabel }}</button></div>
      </form>
    </div>

    <div v-if="showCredentialForm" class="dialog-backdrop" @click.self="showCredentialForm = false">
      <form class="dialog" @submit.prevent="saveCredential">
        <div class="dialog-header"><h2>{{ editingCredential ? '更新 SSH 凭证' : '新增 SSH 凭证' }}</h2><button class="btn btn-ghost btn-small" type="button" @click="showCredentialForm = false">关闭</button></div>
        <div class="dialog-body form-grid">
          <div class="field"><label for="credential-id">凭证 ID</label><input id="credential-id" v-model="credentialForm.id" class="input" :disabled="editingCredential" required /></div>
          <div class="field"><label for="credential-type">类型</label><select id="credential-type" v-model="credentialForm.type" class="select"><option value="password">password</option><option value="key">key</option></select></div>
          <div class="field span-2"><label for="credential-user">用户名</label><input id="credential-user" v-model="credentialForm.username" class="input" required /></div>
          <div v-if="credentialForm.type === 'password'" class="field span-2"><label for="credential-password">密码</label><input id="credential-password" v-model="credentialForm.password" class="input" type="password" autocomplete="new-password" /></div>
          <template v-else>
            <div class="field span-2"><label for="credential-key">私钥内容或路径</label><textarea id="credential-key" v-model="credentialForm.private_key" class="textarea mono" /></div>
            <div class="field span-2"><label for="credential-passphrase">Passphrase（可选）</label><input id="credential-passphrase" v-model="credentialForm.passphrase" class="input" type="password" autocomplete="new-password" /></div>
          </template>
        </div>
        <div class="dialog-footer"><button class="btn" type="button" @click="showCredentialForm = false">取消</button><button class="btn btn-primary" type="submit" :disabled="inventory.saving">保存凭证</button></div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.resource-tabs { display: flex; gap: 5px; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.resource-tabs button { display: flex; align-items: center; gap: 7px; padding: 9px 12px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-secondary); }
.resource-tabs button.active { border-bottom-color: var(--accent); color: var(--text-primary); }
.resource-tabs span { padding: 1px 5px; border-radius: 999px; background: var(--bg-tertiary); color: var(--text-muted); font-size: 10px; }
.server-grid { grid-template-columns: repeat(auto-fill, minmax(300px, 380px)); justify-content: start; }
.server-card { padding: 15px; }
.server-card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.server-card-top code { display: block; margin-top: 5px; color: var(--text-muted); font-size: 12px; }
.server-title { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; }
.resource-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.resource-meta div { min-width: 0; }
.resource-meta dt { color: var(--text-muted); font-size: 10px; text-transform: uppercase; }
.resource-meta dd { margin: 3px 0 0; overflow: hidden; color: var(--text-secondary); text-overflow: ellipsis; white-space: nowrap; }
.verification-time { display: block; margin-top: 4px; color: var(--text-muted); white-space: nowrap; }
.profile-metadata { min-height: 38px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 7px 9px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); color: var(--text-muted); font-size: 10px; }
.card-actions { display: flex; justify-content: flex-end; gap: 7px; padding-top: 12px; border-top: 1px solid var(--border); }
.resource-empty { grid-column: 1 / -1; }
.service-dialog { width: min(920px, 100%); }
.server-checkboxes { min-height: 38px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; padding: 7px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); }
.server-checkboxes label { display: inline-flex; align-items: center; gap: 5px; padding: 4px 7px; border-radius: 5px; background: var(--bg-tertiary); color: var(--text-secondary); font-size: 11px; }
.server-checkboxes input { accent-color: var(--accent); }
.server-connection-check { min-height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); }
.server-connection-check > div { min-width: 0; display: grid; gap: 3px; }
.server-connection-check strong { color: var(--text-secondary); font-size: 11px; }
.server-connection-check small { color: var(--text-muted); overflow-wrap: anywhere; }
.server-connection-check[data-state='testing'] { border-color: var(--warning); }
.server-connection-check[data-state='success'] { border-color: var(--success); }
.server-connection-check[data-state='success'] small { color: var(--success); }
.server-connection-check[data-state='error'] { border-color: var(--danger); }
.server-connection-check[data-state='error'] small { color: var(--danger); }
@media (max-width: 720px) { .server-grid { grid-template-columns: 1fr; } .server-connection-check { align-items: stretch; flex-direction: column; } .server-connection-check .btn { width: 100%; } }
</style>
