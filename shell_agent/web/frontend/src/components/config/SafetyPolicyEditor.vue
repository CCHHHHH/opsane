<script setup lang="ts">
import { reactive, ref, watch } from 'vue'

import { errorMessage } from '../../api/http'
import { useSettingsStore, type SafetyEnvironmentPolicy, type SafetyRule } from '../../stores/settings'
import ConfigTip from './ConfigTip.vue'

interface EnvironmentRow {
  name: string
  requireSecondary: boolean
  levels: string
  executors: string
  timeWindowJson: string
}

const settings = useSettingsStore()
const environments = ref<EnvironmentRow[]>([])
const safePatterns = ref('')
const forbiddenRules = ref<SafetyRule[]>([])
const newEnvironment = ref('')
const localError = ref('')
const classifier = reactive({ command: 'df -h; useradd backdoor', target: 'test-target', env: 'dev', executor: 'ssh' })

function splitCsv(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

function hydrate() {
  const safety = settings.safety
  if (!safety) return
  environments.value = Object.entries(safety.environments ?? {}).map(([name, policy]) => ({
    name,
    requireSecondary: Boolean(policy.require_secondary_confirm),
    levels: (policy.secondary_confirm_levels ?? []).join(', '),
    executors: (policy.forbidden_executors ?? []).join(', '),
    timeWindowJson: policy.time_window ? JSON.stringify(policy.time_window, null, 2) : '',
  }))
  safePatterns.value = (safety.safe_patterns ?? []).join('\n')
  forbiddenRules.value = (safety.forbidden_patterns ?? []).map((rule) => ({ ...rule }))
}

function addEnvironment() {
  const name = newEnvironment.value.trim().toLowerCase()
  if (!name || environments.value.some((row) => row.name === name)) return
  environments.value.push({ name, requireSecondary: name === 'prod', levels: 'critical, dangerous', executors: '', timeWindowJson: '' })
  newEnvironment.value = ''
}

function addRule() {
  forbiddenRules.value.push({ name: `configured_risk_${forbiddenRules.value.length + 1}`, level: 'dangerous', pattern: '', reason: '命中安全配置中的风险规则' })
}

function parseTimeWindow(row: EnvironmentRow): Record<string, string[]> | undefined {
  if (!row.timeWindowJson.trim()) return undefined
  const value: unknown = JSON.parse(row.timeWindowJson)
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${row.name} 的时间窗口必须是 JSON 对象`)
  const result: Record<string, string[]> = {}
  for (const [key, windows] of Object.entries(value)) {
    if (!Array.isArray(windows) || windows.some((item) => typeof item !== 'string')) {
      throw new Error(`${row.name}.${key} 必须是字符串数组`)
    }
    result[key] = windows
  }
  return result
}

async function save() {
  localError.value = ''
  try {
    const environmentPayload: Record<string, SafetyEnvironmentPolicy> = {}
    for (const row of environments.value) {
      const name = row.name.trim().toLowerCase()
      if (!name) continue
      const timeWindow = parseTimeWindow(row)
      environmentPayload[name] = {
        require_secondary_confirm: row.requireSecondary,
        secondary_confirm_levels: splitCsv(row.levels),
        forbidden_executors: splitCsv(row.executors),
        ...(timeWindow ? { time_window: timeWindow } : {}),
      }
    }
    await settings.saveSafety({
      environments: environmentPayload,
      safe_patterns: safePatterns.value.split('\n').map((item) => item.trim()).filter(Boolean),
      forbidden_patterns: forbiddenRules.value.filter((rule) => rule.pattern.trim()).map((rule) => ({ ...rule })),
    })
    hydrate()
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

function classify() {
  if (!classifier.command.trim()) return
  void settings.classifySafety({ ...classifier, command: classifier.command.trim() })
}

watch(() => settings.safety, hydrate, { immediate: true })
</script>

<template>
  <form @submit.prevent="save">
    <div class="panel-card-header">
      <div>
        <h2 class="panel-card-title">安全策略</h2>
        <small class="text-muted">环境策略 {{ settings.safety?.environment_source ?? '-' }} · Safe 规则 {{ settings.safety?.safe_source ?? '-' }} · 风险规则 {{ settings.safety?.forbidden_source ?? '-' }}</small>
      </div>
      <button class="btn btn-primary btn-small" type="submit" :disabled="settings.saving">保存策略</button>
    </div>

    <div v-if="localError" class="notice notice-error safety-notice">{{ localError }}</div>

    <div class="safety-section">
      <div class="section-heading"><div><h3>环境策略</h3><p>配置二次确认、禁用执行器及风险命令允许窗口。</p></div><div class="inline-actions"><input v-model="newEnvironment" class="input new-env-input" placeholder="环境名" title="新增策略使用的环境名称，例如 prod 或 prod-gray。" @keyup.enter.prevent="addEnvironment" /><button class="btn btn-small" type="button" @click="addEnvironment">添加</button></div></div>
      <div class="data-table-wrap">
        <table class="data-table safety-table">
          <thead><tr><th><span class="table-heading">环境 <ConfigTip text="与服务器环境标签匹配，每个环境可使用独立安全策略。" /></span></th><th><span class="table-heading">二次确认 <ConfigTip text="开启后，命中指定风险等级的命令需要额外输入确认短语。" /></span></th><th><span class="table-heading">确认等级 <ConfigTip text="逗号分隔风险等级，仅在二次确认开启时生效。" /></span></th><th><span class="table-heading">禁用执行器 <ConfigTip text="逗号分隔执行器名称；这些执行器在该环境中会被策略阻断。" /></span></th><th><span class="table-heading">时间窗口 JSON <ConfigTip text="按风险动作配置允许时间段；留空表示不限制执行时段。" /></span></th><th /></tr></thead>
          <tbody>
            <tr v-for="(row, index) in environments" :key="`${row.name}-${index}`">
              <td><input v-model="row.name" class="input table-input mono" /></td>
              <td class="center"><input v-model="row.requireSecondary" type="checkbox" /></td>
              <td><input v-model="row.levels" class="input table-input" placeholder="critical, dangerous" /></td>
              <td><input v-model="row.executors" class="input table-input" placeholder="kubectl, local" /></td>
              <td><textarea v-model="row.timeWindowJson" class="textarea table-json mono" placeholder='{"dangerous_allowed":["10:00-18:00"]}' /></td>
              <td><button class="btn btn-danger btn-small" type="button" @click="environments.splice(index, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="safety-columns">
      <div class="safety-section">
        <div class="section-heading"><div><h3 class="heading-with-tip">Safe 命令正则 <ConfigTip text="命中且未触发更高风险规则时判定为 safe，可在自动安全模式下直接执行。" /></h3><p>每行一个完整正则表达式。</p></div></div>
        <textarea v-model="safePatterns" class="textarea mono patterns-editor" spellcheck="false" />
      </div>
      <div class="safety-section">
        <div class="section-heading"><div><h3>分类测试</h3><p>保存前可验证命令与环境策略结果。</p></div></div>
        <div class="form-grid classify-form">
          <div class="field span-2"><div class="field-heading"><label for="safety-command">命令</label><ConfigTip text="输入待验证命令，仅运行风险分类，不会实际执行。" /></div><input id="safety-command" v-model="classifier.command" class="input mono" /></div>
          <div class="field"><div class="field-heading"><label for="safety-target">目标</label><ConfigTip text="模拟的目标服务器别名，用于匹配目标相关策略。" /></div><input id="safety-target" v-model="classifier.target" class="input" /></div>
          <div class="field"><div class="field-heading"><label for="safety-env">环境</label><ConfigTip text="选择用于本次分类测试的环境策略。" /></div><select id="safety-env" v-model="classifier.env" class="select"><option v-for="row in environments" :key="row.name" :value="row.name">{{ row.name }}</option><option v-if="!environments.length" value="dev">dev</option></select></div>
        </div>
        <button class="btn btn-small" type="button" @click="classify">运行分类测试</button>
        <pre v-if="settings.classification" class="code-block classify-result">{{ JSON.stringify(settings.classification, null, 2) }}</pre>
      </div>
    </div>

    <div class="safety-section">
      <div class="section-heading"><div><h3>风险规则</h3><p>按优先级匹配危险或关键命令模式。</p></div><button class="btn btn-small" type="button" @click="addRule">添加规则</button></div>
      <div class="data-table-wrap">
        <table class="data-table safety-table">
          <thead><tr><th><span class="table-heading">名称 <ConfigTip text="规则的唯一可读名称，用于审计和风险说明。" /></span></th><th><span class="table-heading">等级 <ConfigTip text="caution 表示提醒，dangerous 需要确认，critical 表示最高风险。" /></span></th><th><span class="table-heading">正则 <ConfigTip text="用于匹配实际 Shell 命令的正则表达式。" /></span></th><th><span class="table-heading">原因 <ConfigTip text="命中规则后展示给用户的风险解释。" /></span></th><th /></tr></thead>
          <tbody>
            <tr v-for="(rule, index) in forbiddenRules" :key="`${rule.name}-${index}`">
              <td><input v-model="rule.name" class="input table-input mono" /></td>
              <td><select v-model="rule.level" class="select table-input"><option value="caution">caution</option><option value="dangerous">dangerous</option><option value="critical">critical</option></select></td>
              <td><input v-model="rule.pattern" class="input table-input mono" /></td>
              <td><input v-model="rule.reason" class="input table-input" /></td>
              <td><button class="btn btn-danger btn-small" type="button" @click="forbiddenRules.splice(index, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </form>
</template>

<style scoped>
.safety-notice { margin: 12px; }
.safety-section { padding: 16px; border-bottom: 1px solid var(--border); }
.safety-section:last-child { border-bottom: 0; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.section-heading h3 { margin: 0; font-size: 13px; }
.section-heading p { margin: 4px 0 0; color: var(--text-muted); font-size: 11px; }
.heading-with-tip, .field-heading, .table-heading { display: inline-flex; align-items: center; gap: 5px; }
.field-heading { color: var(--text-secondary); font-size: 12px; font-weight: 500; }
.field-heading label { color: inherit; }
.table-heading { white-space: nowrap; }
.new-env-input { width: 130px; min-height: 29px; }
.safety-table { min-width: 900px; }
.table-input { min-width: 130px; min-height: 30px; padding: 4px 7px; font-size: 11px; }
.table-json { min-width: 250px; min-height: 62px; padding: 6px; font-size: 10px; }
.center { text-align: center; }
.center input { accent-color: var(--accent); }
.safety-columns { display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid var(--border); }
.safety-columns .safety-section { border-right: 1px solid var(--border); border-bottom: 0; }
.safety-columns .safety-section:last-child { border-right: 0; }
.patterns-editor { min-height: 210px; font-size: 11px; }
.classify-form { margin-bottom: 9px; }
.classify-result { max-height: 230px; margin-top: 10px; font-size: 10px; }
@media (max-width: 900px) { .safety-columns { grid-template-columns: 1fr; } .safety-columns .safety-section { border-right: 0; border-bottom: 1px solid var(--border); } }
</style>
