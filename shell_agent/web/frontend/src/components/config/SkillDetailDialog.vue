<script setup lang="ts">
import { ref, watch } from 'vue'

import { errorMessage } from '../../api/http'
import { useNotificationsStore } from '../../stores/notifications'
import { useSettingsStore } from '../../stores/settings'
import type { SkillPreview } from '../../stores/settings'
import { confirmAction } from '../../utils/confirm'

const props = withDefaults(defineProps<{ name: string; creating?: boolean }>(), { creating: false })
const emit = defineEmits<{ close: []; saved: [] }>()
const settings = useSettingsStore()
const notifications = useNotificationsStore()
const editing = ref(false)
const yaml = ref('')
const localError = ref('')
const previewInput = ref('')
const preview = ref<SkillPreview | null>(null)

const starterYaml = `name: new_skill
version: "1"
description: 新的确定性运维能力
category: custom
enabled: true
triggers:
  - 示例触发词
params:
  - name: target
    type: server_alias
    required: true
steps:
  - name: 检查服务器状态
    command: ssh {{target}} 'uptime'
    confirm: true
    timeout_seconds: 30
    on_failure: abort
safety:
  default_confirm_mode: interactive
`

watch(() => props.name, async (name) => {
  editing.value = props.creating
  localError.value = ''
  preview.value = null
  if (props.creating) {
    settings.clearSkill()
    yaml.value = starterYaml
    return
  }
  await settings.loadSkill(name)
  yaml.value = settings.skillYaml
}, { immediate: true })

async function save() {
  localError.value = ''
  if (!yaml.value.trim()) {
    localError.value = 'YAML 不能为空'
    return
  }
  try {
    if (props.creating) await settings.createSkill(yaml.value)
    else await settings.saveSkill(props.name, yaml.value)
    editing.value = false
    emit('saved')
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function runPreview() {
  localError.value = ''
  preview.value = null
  if (!previewInput.value.trim()) {
    localError.value = '请输入用于命中 Skill 的测试对话'
    return
  }
  try {
    preview.value = await settings.previewSkill(yaml.value || settings.skillYaml, previewInput.value)
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function removeSkill() {
  if (!props.name || !confirmAction(`确定删除 Skill ${props.name} 吗？`)) return
  try {
    await settings.deleteSkill(props.name)
    emit('saved')
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function copyYaml() {
  if (!settings.skillYaml) return
  try {
    await navigator.clipboard.writeText(settings.skillYaml)
    notifications.success('Skill YAML 已复制')
  } catch (error) {
    notifications.error(`复制失败：${errorMessage(error)}`)
  }
}
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')">
    <div class="dialog skill-dialog">
      <div class="dialog-header">
        <div><span class="text-muted skill-eyebrow">Template Skill</span><h2>{{ creating ? '新建 Skill' : name }}</h2></div>
        <button class="btn btn-ghost btn-small" type="button" @click="emit('close')">关闭</button>
      </div>
      <div v-if="settings.skillLoading" class="loading-state"><span class="spinner" /><span>正在加载 Skill…</span></div>
      <div v-else class="dialog-body">
        <div v-if="localError" class="notice notice-error">{{ localError }}</div>
        <template v-if="settings.skillDetail || creating">
          <div v-if="!editing" class="skill-summary">
            <dl>
              <div><dt>分类</dt><dd>{{ settings.skillDetail?.category || '-' }}</dd></div>
              <div><dt>版本</dt><dd>{{ settings.skillDetail?.version || '1' }}</dd></div>
              <div><dt>状态</dt><dd>{{ settings.skillDetail?.enabled === false ? '停用' : '启用' }}</dd></div>
              <div class="wide"><dt>描述</dt><dd>{{ settings.skillDetail?.description || '-' }}</dd></div>
              <div class="wide"><dt>触发词</dt><dd>{{ (settings.skillDetail?.triggers ?? []).join('、') || '-' }}</dd></div>
              <div class="wide"><dt>来源</dt><dd><code>{{ settings.skillDetail?.source || '-' }}</code></dd></div>
            </dl>
            <div v-if="settings.skillDetail?.step_items?.length" class="skill-steps">
              <h3>步骤</h3>
              <article v-for="(step, index) in settings.skillDetail?.step_items" :key="index">
                <strong>{{ index + 1 }}. {{ String(step.name ?? step.intent ?? '未命名步骤') }}</strong>
                <pre class="code-block">{{ String(step.command ?? '') }}</pre>
                <p v-if="step.explanation">{{ String(step.explanation) }}</p>
              </article>
            </div>
            <h3>原始 YAML</h3>
            <pre class="code-block yaml-preview">{{ settings.skillYaml }}</pre>
          </div>
          <div v-else class="field">
            <label for="skill-yaml">直接编辑 YAML；已有 Skill 不允许修改 name。命令变量必须声明类型。</label>
            <textarea id="skill-yaml" v-model="yaml" class="textarea mono yaml-editor" spellcheck="false" />
          </div>
          <section class="skill-preview-panel">
            <h3>安全试运行</h3>
            <p class="text-muted">只做触发匹配、参数渲染和风险分类，绝不执行命令。</p>
            <div class="preview-input-row">
              <input v-model="previewInput" class="input" placeholder="例如：查看 dev-01 的资源情况" @keyup.enter="runPreview" />
              <button class="btn" type="button" :disabled="settings.skillLoading" @click="runPreview">预览</button>
            </div>
            <div v-if="preview" class="preview-results">
              <div><strong>参数：</strong><code>{{ JSON.stringify(preview.params) }}</code></div>
              <div v-if="preview.missing_params.length" class="notice notice-warning">缺少参数：{{ preview.missing_params.join('、') }}</div>
              <article v-for="step in preview.steps" :key="String(step.index)">
                <div><strong>{{ step.index }}. {{ step.skill_step_name }}</strong> <span class="badge">{{ step.risk_level }}</span></div>
                <pre class="code-block">{{ step.command }}</pre>
                <small>确认：{{ step.confirm ? '需要' : '按策略' }} · 超时：{{ step.timeout_seconds ?? '默认' }} 秒 · 失败：{{ step.on_failure }}</small>
              </article>
            </div>
          </section>
        </template>
      </div>
      <div class="dialog-footer">
        <button v-if="!creating && !editing" class="btn btn-danger" type="button" :disabled="settings.skillLoading" @click="removeSkill">删除</button>
        <button v-if="!creating && !editing" class="btn" type="button" :disabled="!settings.skillYaml" @click="copyYaml">复制 YAML</button>
        <span class="dialog-spacer" />
        <button v-if="editing && !creating" class="btn" type="button" @click="editing = false; yaml = settings.skillYaml">取消编辑</button>
        <button v-if="!editing" class="btn" type="button" :disabled="!settings.skillDetail" @click="editing = true">编辑 YAML</button>
        <button v-else class="btn btn-primary" type="button" :disabled="settings.skillLoading" @click="save">{{ creating ? '创建' : '保存' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.skill-dialog { width: min(920px, 100%); }
.skill-eyebrow { display: block; margin-bottom: 3px; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.skill-summary dl { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0 0 18px; }
.skill-summary dl > div { padding: 9px; border: 1px solid var(--border); border-radius: 7px; }
.skill-summary dl .wide { grid-column: span 2; }
.skill-summary dt { color: var(--text-muted); font-size: 10px; text-transform: uppercase; }
.skill-summary dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.skill-summary h3 { margin: 17px 0 9px; font-size: 13px; }
.skill-steps { display: grid; gap: 8px; }
.skill-steps article { padding: 11px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-primary); }
.skill-steps .code-block { margin-top: 8px; }
.skill-steps p { margin: 7px 0 0; color: var(--text-secondary); }
.yaml-preview { max-height: 300px; }
.dialog-spacer { flex: 1; }
.yaml-editor { min-height: 520px; font-size: 11px; }
.skill-preview-panel { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border); }
.skill-preview-panel h3 { margin: 0 0 5px; font-size: 13px; }
.skill-preview-panel p { margin: 0 0 10px; }
.preview-input-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
.preview-results { display: grid; gap: 8px; margin-top: 10px; }
.preview-results article { padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-primary); }
.preview-results .code-block { margin: 7px 0; }
</style>
