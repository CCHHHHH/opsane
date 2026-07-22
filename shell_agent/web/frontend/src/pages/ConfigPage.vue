<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import AsyncState from '../components/common/AsyncState.vue'
import PageHeader from '../components/common/PageHeader.vue'
import ConfigTip from '../components/config/ConfigTip.vue'
import SafetyPolicyEditor from '../components/config/SafetyPolicyEditor.vue'
import SkillDetailDialog from '../components/config/SkillDetailDialog.vue'
import { useSettingsStore } from '../stores/settings'
import { confirmAction } from '../utils/confirm'

type ConfigTab = 'llm' | 'context' | 'ssh' | 'safety' | 'skills'

const settings = useSettingsStore()
const activeTab = ref<ConfigTab>('llm')
const selectedSkill = ref('')
const scanningSkills = ref(false)
const llm = reactive({
  provider: 'openai',
  model: '',
  summary_model: '',
  api_key: '',
  api_key_set: false,
  base_url: '',
  temperature: 0.3,
  timeout: 60,
})
const context = reactive({
  semantic_summary_enabled: true,
  summary_trigger_events: 16,
  summary_trigger_chars: 12000,
  recent_events: 8,
  summary_max_chars: 3200,
  summary_max_tokens: 1200,
})
const ssh = reactive({
  max_per_host: 3,
  idle_timeout: 300,
  total_max: 50,
  default_timeout: 60,
  trust_unknown_hosts: true,
})

function hydrate() {
  if (!settings.config) return
  const sourceLlm = settings.config.llm
  Object.assign(llm, {
    provider: String(sourceLlm.provider ?? 'openai'),
    model: String(sourceLlm.model ?? ''),
    summary_model: String(sourceLlm.summary_model ?? ''),
    api_key: '',
    api_key_set: Boolean(sourceLlm.api_key_set),
    base_url: String(sourceLlm.base_url ?? ''),
    temperature: Number(sourceLlm.temperature ?? 0.3),
    timeout: Number(sourceLlm.timeout ?? 60),
  })
  const sourceContext = settings.config.context ?? {}
  Object.assign(context, {
    semantic_summary_enabled: Boolean(sourceContext.semantic_summary_enabled ?? true),
    summary_trigger_events: Number(sourceContext.summary_trigger_events ?? 16),
    summary_trigger_chars: Number(sourceContext.summary_trigger_chars ?? 12000),
    recent_events: Number(sourceContext.recent_events ?? 8),
    summary_max_chars: Number(sourceContext.summary_max_chars ?? 3200),
    summary_max_tokens: Number(sourceContext.summary_max_tokens ?? 1200),
  })
  const sourceSsh = settings.config.ssh
  Object.assign(ssh, {
    max_per_host: Number(sourceSsh.max_per_host ?? 3),
    idle_timeout: Number(sourceSsh.idle_timeout ?? 300),
    total_max: Number(sourceSsh.total_max ?? 50),
    default_timeout: Number(sourceSsh.default_timeout ?? 60),
    trust_unknown_hosts: Boolean(sourceSsh.trust_unknown_hosts ?? true),
  })
}

async function reload() {
  await Promise.all([settings.load(), settings.loadSkillCandidates()])
  hydrate()
}

async function scanSkillHistory(semantic = true) {
  scanningSkills.value = true
  try {
    await settings.scanSkillCandidates(30, 3, semantic)
  } finally {
    scanningSkills.value = false
  }
}

async function acceptCandidate(id: string, name: string) {
  if (!confirmAction(`确认把候选 ${name} 创建为停用 Skill 吗？创建后仍需人工检查并手动启用。`)) return
  await settings.acceptSkillCandidate(id)
}

async function rejectCandidate(id: string, name: string) {
  if (!confirmAction(`确定拒绝 Skill 候选 ${name} 吗？`)) return
  await settings.rejectSkillCandidate(id)
}

function evidenceTargets(evidence: Record<string, unknown>): string {
  const targets = evidence.targets
  return Array.isArray(targets) ? targets.map(String).join('、') : ''
}

function evidenceMode(evidence: Record<string, unknown>): string {
  return evidence.grouping_mode === 'semantic' ? '语义归组' : '精确归组'
}

function evidenceParams(evidence: Record<string, unknown>): string {
  const params = evidence.parameterized_fields
  return Array.isArray(params) ? params.map(String).join('、') : ''
}

async function saveLlm() {
  await settings.saveSection('llm', {
    provider: llm.provider,
    model: llm.model,
    summary_model: llm.summary_model,
    api_key: llm.api_key,
    base_url: llm.base_url,
    temperature: llm.temperature,
    timeout: llm.timeout,
  })
  hydrate()
}

async function saveContext() {
  await settings.saveSection('context', { ...context })
  hydrate()
}

async function saveSsh() {
  await settings.saveSection('ssh', { ...ssh })
  hydrate()
}

function testLlm() {
  void settings.testLlm({
    provider: llm.provider,
    model: llm.model,
    api_key: llm.api_key,
    base_url: llm.base_url,
  })
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <section class="page-shell">
    <div class="page-container config-layout">
      <PageHeader title="系统配置" description="管理 LLM、SSH 连接池与 Template Skill；敏感字段不会从服务端回显。">
        <template #actions>
          <button class="btn" type="button" :disabled="settings.loading" @click="reload">刷新</button>
        </template>
      </PageHeader>

      <div v-if="settings.error" class="notice notice-error">{{ settings.error }}</div>
      <div v-else-if="settings.notice" class="notice notice-success">{{ settings.notice }}</div>

      <div class="config-grid">
        <nav class="config-nav" aria-label="配置类别">
          <button type="button" :class="{ active: activeTab === 'llm' }" @click="activeTab = 'llm'">
            <strong>LLM</strong><span>模型与 API</span>
          </button>
          <button type="button" :class="{ active: activeTab === 'context' }" @click="activeTab = 'context'">
            <strong>上下文</strong><span>语义摘要策略</span>
          </button>
          <button type="button" :class="{ active: activeTab === 'ssh' }" @click="activeTab = 'ssh'">
            <strong>SSH</strong><span>连接池策略</span>
          </button>
          <button type="button" :class="{ active: activeTab === 'safety' }" @click="activeTab = 'safety'">
            <strong>安全策略</strong><span>风险与环境门禁</span>
          </button>
          <button type="button" :class="{ active: activeTab === 'skills' }" @click="activeTab = 'skills'">
            <strong>Skills</strong><span>模板能力</span>
          </button>
        </nav>

        <div class="panel-card config-content">
          <AsyncState :loading="settings.loading" :error="settings.error && !settings.config ? settings.error : ''" @retry="reload">
            <form v-if="activeTab === 'llm'" @submit.prevent="saveLlm">
              <div class="panel-card-header">
                <div>
                  <h2 class="panel-card-title">LLM 配置</h2>
                  <small class="text-muted">用于命令生成、分析与操作方案。</small>
                </div>
                <span class="badge" :class="llm.api_key_set ? 'badge-success' : 'badge-warning'">
                  {{ llm.api_key_set ? 'Key 已配置' : 'Key 未配置' }}
                </span>
              </div>
              <div class="panel-card-body form-grid">
                <div class="field">
                  <div class="field-heading"><label for="llm-provider">Provider</label><ConfigTip text="选择模型服务协议，需与 API Key 和 Base URL 对应。" /></div>
                  <input id="llm-provider" v-model="llm.provider" class="input" autocomplete="off" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="llm-model">模型</label><ConfigTip text="填写服务端实际模型标识，例如 gpt-4.1 或 deepseek-chat。" /></div>
                  <input id="llm-model" v-model="llm.model" class="input" autocomplete="off" required />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="llm-summary-model">摘要模型</label><ConfigTip text="专门用于上下文压缩；留空时复用主模型。" /></div>
                  <input id="llm-summary-model" v-model="llm.summary_model" class="input" autocomplete="off" placeholder="留空时复用主模型" />
                </div>
                <div class="field span-2">
                  <div class="field-heading"><label for="llm-key">API Key</label><ConfigTip text="用于调用模型服务；密钥由后端保存，页面不会回显原值。" /></div>
                  <input id="llm-key" v-model="llm.api_key" class="input" type="password" :placeholder="llm.api_key_set ? '已配置，留空不修改' : ''" autocomplete="new-password" />
                </div>
                <div class="field span-2">
                  <div class="field-heading"><label for="llm-url">Base URL</label><ConfigTip text="模型 API 根地址；使用官方默认地址时留空，代理或私有部署时填写。" /></div>
                  <input id="llm-url" v-model="llm.base_url" class="input" type="url" placeholder="使用 Provider 默认地址时可留空" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="llm-temperature">Temperature</label><ConfigTip text="控制输出随机性；运维命令建议使用较低值以提高稳定性。" /></div>
                  <input id="llm-temperature" v-model.number="llm.temperature" class="input" type="number" min="0" max="2" step="0.1" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="llm-timeout">超时（秒）</label><ConfigTip text="单次 LLM 请求的等待上限，超过后当前模型调用失败。" /></div>
                  <input id="llm-timeout" v-model.number="llm.timeout" class="input" type="number" min="1" />
                </div>
              </div>
              <div class="config-footer">
                <button class="btn" type="button" :disabled="settings.saving" @click="testLlm">测试连接</button>
                <button class="btn btn-primary" type="submit" :disabled="settings.saving">保存 LLM 配置</button>
              </div>
            </form>

            <form v-else-if="activeTab === 'context'" @submit.prevent="saveContext">
              <div class="panel-card-header">
                <div>
                  <h2 class="panel-card-title">上下文语义摘要</h2>
                  <small class="text-muted">较早事件由大模型增量总结，最近事件继续保留原文。</small>
                </div>
                <span class="badge" :class="context.semantic_summary_enabled ? 'badge-success' : 'badge-warning'">
                  {{ context.semantic_summary_enabled ? '已启用' : '已停用' }}
                </span>
              </div>
              <div class="panel-card-body form-grid">
                <label class="check-row span-2">
                  <input v-model="context.semantic_summary_enabled" type="checkbox" />
                  <span><strong class="inline-heading">启用增量语义摘要 <ConfigTip text="达到阈值后压缩较早对话，最近事件继续保留原文；模型失败时自动降级。" /></strong><small>模型失败时自动降级到规则压缩。</small></span>
                </label>
                <div class="field">
                  <div class="field-heading"><label for="context-trigger-events">事件数阈值</label><ConfigTip text="累计会话事件达到该数量时触发摘要检查。" /></div>
                  <input id="context-trigger-events" v-model.number="context.summary_trigger_events" class="input" type="number" min="4" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="context-trigger-chars">字符数阈值</label><ConfigTip text="上下文原文累计达到该字符数时触发摘要检查。" /></div>
                  <input id="context-trigger-chars" v-model.number="context.summary_trigger_chars" class="input" type="number" min="2000" step="500" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="context-recent-events">保留最近事件</label><ConfigTip text="摘要时保留的最近原始事件数量；值越大上下文更完整，但占用更多。" /></div>
                  <input id="context-recent-events" v-model.number="context.recent_events" class="input" type="number" min="1" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="context-max-chars">摘要最大字符</label><ConfigTip text="写入会话上下文的语义摘要文本最大长度。" /></div>
                  <input id="context-max-chars" v-model.number="context.summary_max_chars" class="input" type="number" min="800" step="100" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="context-max-tokens">摘要输出 Token</label><ConfigTip text="摘要模型单次输出上限，影响摘要完整度、耗时和调用成本。" /></div>
                  <input id="context-max-tokens" v-model.number="context.summary_max_tokens" class="input" type="number" min="128" step="64" />
                </div>
              </div>
              <div class="config-footer">
                <button class="btn btn-primary" type="submit" :disabled="settings.saving">保存上下文配置</button>
              </div>
            </form>

            <form v-else-if="activeTab === 'ssh'" @submit.prevent="saveSsh">
              <div class="panel-card-header">
                <div>
                  <h2 class="panel-card-title">SSH 连接池</h2>
                  <small class="text-muted">修改后由后端重新加载运行配置。</small>
                </div>
              </div>
              <div class="panel-card-body form-grid">
                <div class="field">
                  <div class="field-heading"><label for="ssh-per-host">单主机最大连接</label><ConfigTip text="同一服务器允许复用的最大 SSH 连接数。" /></div>
                  <input id="ssh-per-host" v-model.number="ssh.max_per_host" class="input" type="number" min="1" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="ssh-total">全局最大连接</label><ConfigTip text="所有服务器连接池的合计上限，用于限制并发资源占用。" /></div>
                  <input id="ssh-total" v-model.number="ssh.total_max" class="input" type="number" min="1" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="ssh-idle">空闲回收（秒）</label><ConfigTip text="连接空闲超过该时间后回收，下次操作会重新建立连接。" /></div>
                  <input id="ssh-idle" v-model.number="ssh.idle_timeout" class="input" type="number" min="1" />
                </div>
                <div class="field">
                  <div class="field-heading"><label for="ssh-timeout">默认命令超时（秒）</label><ConfigTip text="命令执行的默认等待上限，超过后停止等待并标记为超时。" /></div>
                  <input id="ssh-timeout" v-model.number="ssh.default_timeout" class="input" type="number" min="1" />
                </div>
                <label class="check-row span-2">
                  <input v-model="ssh.trust_unknown_hosts" type="checkbox" />
                  <span><strong class="inline-heading">信任未知主机密钥 <ConfigTip text="首次连接时自动接受未知主机指纹；生产环境建议关闭并维护 known_hosts。" /></strong><small>仅在可信网络中启用；生产环境建议维护 known_hosts。</small></span>
                </label>
              </div>
              <div class="config-footer">
                <button class="btn btn-primary" type="submit" :disabled="settings.saving">保存 SSH 配置</button>
              </div>
            </form>

            <SafetyPolicyEditor v-else-if="activeTab === 'safety'" />

            <div v-else>
              <div class="panel-card-header">
                <div>
                  <h2 class="panel-card-title">Template Skills</h2>
                  <small class="text-muted">当前后端已加载的声明式运维能力。</small>
                </div>
                <div class="skill-header-actions">
                  <span class="badge badge-accent">{{ settings.skills.length }} 个</span>
                  <button class="btn btn-small" type="button" :disabled="scanningSkills" @click="scanSkillHistory(false)">精确扫描</button>
                  <button class="btn btn-small" type="button" :disabled="scanningSkills" @click="scanSkillHistory(true)">{{ scanningSkills ? '扫描中…' : '语义扫描最近 30 天' }}</button>
                  <button class="btn btn-primary btn-small" type="button" @click="selectedSkill = '__new__'">新建 Skill</button>
                </div>
              </div>
              <div class="data-table-wrap skills-table">
                <table class="data-table">
                  <thead><tr><th>名称</th><th>分类</th><th>描述</th><th>触发词</th><th>状态</th><th /></tr></thead>
                  <tbody>
                    <tr v-for="skill in settings.skills" :key="skill.name">
                      <td><code>{{ skill.name }}</code></td>
                      <td>{{ String(skill.category ?? '-') }}</td>
                      <td>{{ skill.description || '-' }}</td>
                      <td>{{ (skill.triggers ?? []).join('、') || '-' }}</td>
                      <td><span class="badge" :class="skill.enabled === false ? 'badge-warning' : 'badge-success'">{{ skill.enabled === false ? '停用' : '启用' }}</span></td>
                      <td><button class="btn btn-small" type="button" @click="selectedSkill = skill.name">详情 / 编辑</button></td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <section class="skill-candidates-section">
                <div class="candidate-heading">
                  <div>
                    <h3>历史候选</h3>
                    <small class="text-muted">语义扫描只向已配置模型发送脱敏任务摘要；模型仅分组，命令模板由本地确定性编译，批准后仍只创建停用 Skill。</small>
                  </div>
                  <span class="badge" :class="settings.skillCandidates.length ? 'badge-warning' : ''">{{ settings.skillCandidates.length }} 待审核</span>
                </div>
                <div v-if="settings.skillCandidatesLoading" class="loading-state"><span class="spinner" /><span>正在加载候选…</span></div>
                <div v-else-if="!settings.skillCandidates.length" class="empty-state compact-empty">暂无待审核候选，可通过对话或上方按钮扫描历史任务。</div>
                <div v-else class="candidate-list">
                  <details v-for="candidate in settings.skillCandidates" :key="candidate.id" class="candidate-card">
                    <summary>
                      <span><strong>{{ candidate.name }}</strong><small>{{ candidate.description }}</small></span>
                      <span class="candidate-metrics">
                        <span class="badge">{{ candidate.occurrence_count }} 次</span>
                        <span class="badge" :class="candidate.risk_level === 'dangerous' ? 'badge-warning' : 'badge-success'">{{ candidate.risk_level }}</span>
                        <span>{{ Math.round(candidate.confidence * 100) }}%</span>
                      </span>
                    </summary>
                    <div class="candidate-body">
                      <div class="candidate-evidence">
                        <strong>证据</strong>
                        <span>分组：{{ evidenceMode(candidate.evidence) }}</span>
                        <span>目标：{{ evidenceTargets(candidate.evidence) || '-' }}</span>
                        <span v-if="evidenceParams(candidate.evidence)">参数化：{{ evidenceParams(candidate.evidence) }}</span>
                        <span>来源任务：{{ candidate.source_task_ids.length }} 个</span>
                      </div>
                      <pre class="code-block candidate-yaml">{{ candidate.draft_yaml }}</pre>
                      <div v-if="settings.skillCandidatePreviews[candidate.id]" class="candidate-preview">
                        <strong>安全预览（不会执行）</strong>
                        <article v-for="step in settings.skillCandidatePreviews[candidate.id].steps" :key="String(step.index)">
                          <span>{{ step.index }}. {{ step.skill_step_name }} · {{ step.risk_level }}</span>
                          <pre class="code-block">{{ step.command }}</pre>
                        </article>
                      </div>
                      <div class="candidate-actions">
                        <button class="btn" type="button" @click="settings.previewSkillCandidate(candidate.id)">安全预览</button>
                        <button class="btn" type="button" @click="rejectCandidate(candidate.id, candidate.name)">拒绝</button>
                        <button class="btn btn-primary" type="button" @click="acceptCandidate(candidate.id, candidate.name)">批准并创建停用 Skill</button>
                      </div>
                    </div>
                  </details>
                </div>
              </section>
            </div>
          </AsyncState>
        </div>
      </div>
    </div>
    <SkillDetailDialog v-if="selectedSkill" :name="selectedSkill === '__new__' ? '' : selectedSkill" :creating="selectedSkill === '__new__'" @close="selectedSkill = ''" @saved="selectedSkill = ''" />
  </section>
</template>

<style scoped>
.config-grid { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 14px; }
.config-nav { display: flex; flex-direction: column; gap: 5px; }
.config-nav button { display: grid; gap: 3px; padding: 11px 12px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--text-secondary); text-align: left; }
.config-nav button:hover { background: var(--bg-secondary); }
.config-nav button.active { border-color: var(--border-light); background: var(--bg-secondary); color: var(--text-primary); }
.config-nav span { color: var(--text-muted); font-size: 11px; }
.config-content { min-height: 480px; overflow: hidden; }
.config-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border); }
.field-heading, .inline-heading { display: flex; align-items: center; gap: 5px; color: var(--text-secondary); font-size: 12px; font-weight: 500; }
.field-heading label { color: inherit; }
.inline-heading { width: fit-content; color: var(--text-primary); font-weight: 600; }
.check-row { display: flex; align-items: flex-start; gap: 10px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-primary); }
.check-row input { margin-top: 3px; accent-color: var(--accent); }
.check-row span { display: grid; gap: 3px; }
.check-row small { color: var(--text-muted); }
.skills-table { max-height: calc(100vh - 250px); }
.skill-header-actions { display: flex; align-items: center; gap: 8px; }
.skill-candidates-section { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
.candidate-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.candidate-heading h3 { margin: 0 0 4px; font-size: 14px; }
.candidate-list { display: grid; gap: 8px; }
.candidate-card { border: 1px solid var(--border); border-radius: 9px; background: var(--bg-primary); }
.candidate-card summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; cursor: pointer; }
.candidate-card summary > span:first-child { display: grid; gap: 3px; }
.candidate-card summary small { color: var(--text-muted); }
.candidate-metrics { display: flex; align-items: center; gap: 7px; color: var(--text-muted); }
.candidate-body { display: grid; gap: 10px; padding: 0 12px 12px; }
.candidate-evidence { display: flex; flex-wrap: wrap; gap: 12px; color: var(--text-secondary); font-size: 12px; }
.candidate-yaml { max-height: 320px; overflow: auto; }
.candidate-actions { display: flex; justify-content: flex-end; gap: 8px; }
.candidate-preview { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-secondary); }
.candidate-preview article { display: grid; gap: 5px; }
.candidate-preview .code-block { margin: 0; }
.compact-empty { padding: 18px; }
@media (max-width: 760px) { .config-grid { grid-template-columns: 1fr; } .config-nav { flex-direction: row; } .config-nav button { flex: 1; } }
</style>
