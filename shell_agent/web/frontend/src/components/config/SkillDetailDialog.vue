<script setup lang="ts">
import { ref, watch } from 'vue'

import { errorMessage } from '../../api/http'
import { useNotificationsStore } from '../../stores/notifications'
import { useSettingsStore } from '../../stores/settings'

const props = defineProps<{ name: string }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const settings = useSettingsStore()
const notifications = useNotificationsStore()
const editing = ref(false)
const yaml = ref('')
const localError = ref('')

watch(() => props.name, async (name) => {
  editing.value = false
  localError.value = ''
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
    await settings.saveSkill(props.name, yaml.value)
    editing.value = false
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
        <div><span class="text-muted skill-eyebrow">Template Skill</span><h2>{{ name }}</h2></div>
        <button class="btn btn-ghost btn-small" type="button" @click="emit('close')">关闭</button>
      </div>
      <div v-if="settings.skillLoading" class="loading-state"><span class="spinner" /><span>正在加载 Skill…</span></div>
      <div v-else class="dialog-body">
        <div v-if="localError" class="notice notice-error">{{ localError }}</div>
        <template v-if="settings.skillDetail">
          <div v-if="!editing" class="skill-summary">
            <dl>
              <div><dt>分类</dt><dd>{{ settings.skillDetail.category || '-' }}</dd></div>
              <div><dt>状态</dt><dd>{{ settings.skillDetail.enabled === false ? '停用' : '启用' }}</dd></div>
              <div class="wide"><dt>描述</dt><dd>{{ settings.skillDetail.description || '-' }}</dd></div>
              <div class="wide"><dt>触发词</dt><dd>{{ (settings.skillDetail.triggers ?? []).join('、') || '-' }}</dd></div>
              <div class="wide"><dt>来源</dt><dd><code>{{ settings.skillDetail.source || '-' }}</code></dd></div>
            </dl>
            <div v-if="settings.skillDetail.step_items?.length" class="skill-steps">
              <h3>步骤</h3>
              <article v-for="(step, index) in settings.skillDetail.step_items" :key="index">
                <strong>{{ index + 1 }}. {{ String(step.name ?? step.intent ?? '未命名步骤') }}</strong>
                <pre class="code-block">{{ String(step.command ?? '') }}</pre>
                <p v-if="step.explanation">{{ String(step.explanation) }}</p>
              </article>
            </div>
            <h3>原始 YAML</h3>
            <pre class="code-block yaml-preview">{{ settings.skillYaml }}</pre>
          </div>
          <div v-else class="field">
            <label for="skill-yaml">直接编辑 YAML；已有 Skill 不允许修改 name。</label>
            <textarea id="skill-yaml" v-model="yaml" class="textarea mono yaml-editor" spellcheck="false" />
          </div>
        </template>
      </div>
      <div class="dialog-footer">
        <button v-if="!editing" class="btn" type="button" :disabled="!settings.skillYaml" @click="copyYaml">复制 YAML</button>
        <span class="dialog-spacer" />
        <button v-if="editing" class="btn" type="button" @click="editing = false; yaml = settings.skillYaml">取消编辑</button>
        <button v-if="!editing" class="btn" type="button" :disabled="!settings.skillDetail" @click="editing = true">编辑 YAML</button>
        <button v-else class="btn btn-primary" type="button" :disabled="settings.skillLoading" @click="save">保存</button>
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
</style>
