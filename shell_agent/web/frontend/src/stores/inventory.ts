import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { CredentialRecord, ServerRecord, ServiceRecord } from '../api/protocol'
import { useNotificationsStore } from './notifications'

export interface ServerInput {
  alias: string
  host: string
  port: number
  env: string
  role: string
  ssh_credential: string
  tags: string[]
}

export interface CredentialInput {
  id: string
  type: 'password' | 'key'
  username: string
  password?: string
  private_key?: string
  passphrase?: string
}

export interface ServiceInput {
  id: string
  name: string
  env: string
  owners: string[]
  servers: string[]
  deploy_dir: string
  artifact_path: string
  backup_dir: string
  artifact_type: string
  startup_timeout_seconds: number
  log_dir: string
  health_url: string
  ports: number[]
  start_cmd: string
  stop_cmd: string
  restart_cmd: string
  status_cmd: string
  config_paths: string[]
  runtime: string
  version: string
  last_verified_at: string
  verification_status: 'verified' | 'stale' | 'conflicted' | 'unknown'
  source_task_id: string
  revision: number
  tags: string[]
  notes: string
}

export const useInventoryStore = defineStore('inventory', {
  state: () => ({
    servers: [] as ServerRecord[],
    services: [] as ServiceRecord[],
    credentials: [] as CredentialRecord[],
    loading: false,
    saving: false,
    error: '',
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const [servers, services, credentials] = await Promise.all([
          http.get<{ servers: ServerRecord[] }>('/api/servers'),
          http.get<{ services: ServiceRecord[] }>('/api/services'),
          http.get<{ credentials: CredentialRecord[] }>('/api/credentials'),
        ])
        this.servers = servers.servers ?? []
        this.services = services.services ?? []
        this.credentials = credentials.credentials ?? []
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
    async saveServer(input: ServerInput, originalAlias = '') {
      const notifications = useNotificationsStore()
      this.saving = true
      this.error = ''
      try {
        if (originalAlias) {
          await http.put<{ ok: boolean }>(`/api/servers/${encodeURIComponent(originalAlias)}`, input)
        } else {
          await http.post<{ ok: boolean }>('/api/servers', input)
        }
        await this.load()
        notifications.success(originalAlias ? '服务器配置已更新' : '服务器已添加')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.saving = false
      }
    },
    async removeServer(alias: string) {
      const notifications = useNotificationsStore()
      try {
        await http.delete<{ ok: boolean }>(`/api/servers/${encodeURIComponent(alias)}`)
        await this.load()
        notifications.success(`服务器 ${alias} 已删除`)
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      }
    },
    async saveService(input: ServiceInput, originalId = '') {
      const notifications = useNotificationsStore()
      this.saving = true
      this.error = ''
      try {
        if (originalId) {
          await http.put<{ ok: boolean; service: ServiceRecord }>(`/api/services/${encodeURIComponent(originalId)}`, input)
        } else {
          await http.post<{ ok: boolean; service: ServiceRecord }>('/api/services', input)
        }
        await this.load()
        notifications.success(originalId ? '服务画像已更新' : '服务画像已创建')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.saving = false
      }
    },
    async removeService(id: string) {
      const notifications = useNotificationsStore()
      try {
        await http.delete<{ ok: boolean }>(`/api/services/${encodeURIComponent(id)}`)
        await this.load()
        notifications.success('服务画像已删除')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      }
    },
    async saveCredential(input: CredentialInput) {
      const notifications = useNotificationsStore()
      this.saving = true
      try {
        await http.post<{ ok: boolean }>('/api/credentials', input)
        await this.load()
        notifications.success('SSH 凭证已保存')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.saving = false
      }
    },
    async removeCredential(id: string) {
      const notifications = useNotificationsStore()
      try {
        await http.delete<{ ok: boolean }>(`/api/credentials/${encodeURIComponent(id)}`)
        await this.load()
        notifications.success('SSH 凭证已删除')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      }
    },
  },
})
