import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { SkillRecord } from '../api/protocol'
import { useNotificationsStore } from './notifications'

export interface AppConfig {
  llm: Record<string, unknown>
  ssh: Record<string, unknown>
  session: Record<string, unknown>
  context: Record<string, unknown>
}

export interface SafetyEnvironmentPolicy {
  require_secondary_confirm: boolean
  secondary_confirm_levels: string[]
  forbidden_executors: string[]
  time_window?: Record<string, string[]>
}

export interface SafetyRule {
  name: string
  level: 'caution' | 'dangerous' | 'critical'
  pattern: string
  reason: string
}

export interface SafetyConfig {
  environments: Record<string, SafetyEnvironmentPolicy>
  environment_source?: string
  safe_patterns: string[]
  safe_source?: string
  forbidden_patterns: SafetyRule[]
  forbidden_source?: string
}

export interface SafetyClassification {
  ok: boolean
  risk_level?: string
  risk_reasons?: string[]
  risk_rules?: string[]
  policy_blocked?: boolean
  policy_block_reason?: string
  requires_secondary_confirm?: boolean
  secondary_confirm_expected?: string
  [key: string]: unknown
}

export interface SkillDetail extends SkillRecord {
  category?: string
  source?: string
  params?: Array<Record<string, unknown>>
  step_items?: Array<Record<string, unknown>>
  safety?: Record<string, unknown>
}

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    config: null as AppConfig | null,
    safety: null as SafetyConfig | null,
    classification: null as SafetyClassification | null,
    skills: [] as SkillRecord[],
    skillDetail: null as SkillDetail | null,
    skillYaml: '',
    skillLoading: false,
    loading: false,
    saving: false,
    error: '',
    notice: '',
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const [config, skills, safety] = await Promise.all([
          http.get<AppConfig>('/api/config'),
          http.get<{ skills: SkillRecord[] }>('/api/skills'),
          http.get<SafetyConfig>('/api/safety/config'),
        ])
        this.config = config
        this.skills = skills.skills ?? []
        this.safety = safety
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
    async saveSection(section: 'llm' | 'ssh' | 'session' | 'context', data: Record<string, unknown>) {
      const notifications = useNotificationsStore()
      this.saving = true
      this.error = ''
      this.notice = ''
      try {
        await http.put<{ ok: boolean }>('/api/config', { section, data })
        this.notice = '配置已保存'
        await this.load()
        notifications.success(this.notice)
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.saving = false
      }
    },
    async testLlm(data: Record<string, unknown>) {
      const notifications = useNotificationsStore()
      this.saving = true
      this.error = ''
      this.notice = ''
      try {
        const result = await http.post<{ ok: boolean; response?: string; error?: string }>('/api/config/test-llm', data)
        if (!result.ok) throw new Error(result.error || '连接测试失败')
        this.notice = `连接成功${result.response ? `：${result.response}` : ''}`
        notifications.success('LLM 连接测试成功')
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
      } finally {
        this.saving = false
      }
    },
    async saveSafety(config: Pick<SafetyConfig, 'environments' | 'safe_patterns' | 'forbidden_patterns'>) {
      const notifications = useNotificationsStore()
      this.saving = true
      this.error = ''
      this.notice = ''
      try {
        await http.put<{ ok: boolean }>('/api/safety/config', config)
        this.notice = '安全策略已保存'
        this.safety = await http.get<SafetyConfig>('/api/safety/config')
        notifications.success(this.notice)
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.saving = false
      }
    },
    async classifySafety(input: { command: string; target: string; env: string; executor: string }) {
      this.error = ''
      this.classification = null
      try {
        this.classification = await http.post<SafetyClassification>('/api/safety/classify', input)
      } catch (error) {
        this.error = errorMessage(error)
      }
    },
    async loadSkill(name: string) {
      this.skillLoading = true
      this.error = ''
      this.skillDetail = null
      this.skillYaml = ''
      try {
        const result = await http.get<{ ok: boolean; skill: SkillDetail; yaml: string }>(`/api/skills/${encodeURIComponent(name)}`)
        this.skillDetail = result.skill
        this.skillYaml = result.yaml ?? ''
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.skillLoading = false
      }
    },
    async saveSkill(name: string, yaml: string) {
      const notifications = useNotificationsStore()
      this.skillLoading = true
      this.error = ''
      this.notice = ''
      try {
        const result = await http.put<{ ok: boolean; skill: SkillDetail }>(`/api/skills/${encodeURIComponent(name)}`, { yaml })
        this.skillDetail = result.skill
        this.skillYaml = yaml
        this.notice = `Skill ${name} 已保存`
        const skills = await http.get<{ skills: SkillRecord[] }>('/api/skills')
        this.skills = skills.skills ?? []
        notifications.success(this.notice)
      } catch (error) {
        this.error = errorMessage(error)
        notifications.error(this.error)
        throw error
      } finally {
        this.skillLoading = false
      }
    },
    clearSkill() {
      this.skillDetail = null
      this.skillYaml = ''
    },
  },
})
