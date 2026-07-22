<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { errorMessage } from '../api/http'
import type { MemoryRecord, ProfileCandidateRecord } from '../api/protocol'
import AsyncState from '../components/common/AsyncState.vue'
import PageHeader from '../components/common/PageHeader.vue'
import PaginationBar from '../components/common/PaginationBar.vue'
import { useMemoriesStore } from '../stores/memories'
import { confirmAction } from '../utils/confirm'

type KnowledgeTab = 'memories' | 'candidates' | 'issues'

const memories = useMemoriesStore()
const activeTab = ref<KnowledgeTab>('memories')
const showForm = ref(false)
const editingMemoryId = ref<string | number | null>(null)
const selectedCandidate = ref<ProfileCandidateRecord | null>(null)
const candidateDraft = ref('')
const localError = ref('')
const form = reactive({
  subject: '', predicate: 'note', value: '', target: '', type: 'fact',
  status: 'confirmed', confidence: 1, expires_at: '', evidence_summary: '',
})

const candidateChanges = computed(() => Object.entries(selectedCandidate.value?.proposed_changes ?? {}))

function openMemory(item?: MemoryRecord) {
  localError.value = ''
  editingMemoryId.value = item?.id ?? null
  Object.assign(form, {
    subject: item?.subject ?? '',
    predicate: item?.predicate ?? 'note',
    value: item?.value ?? '',
    target: item?.target ?? '',
    type: item?.type ?? 'fact',
    status: item?.status ?? 'confirmed',
    confidence: Number(item?.confidence ?? 1),
    expires_at: item?.expires_at ?? '',
    evidence_summary: item?.evidence_summary ?? '',
  })
  showForm.value = true
}

async function save() {
  localError.value = ''
  if (!form.subject.trim() || !form.value.trim()) {
    localError.value = '主题和值必填'
    return
  }
  const payload = {
    ...form,
    subject: form.subject.trim(),
    predicate: form.predicate.trim() || 'note',
    value: form.value.trim(),
    target: form.target.trim(),
    confidence: Math.max(0, Math.min(Number(form.confidence), 1)),
  }
  try {
    if (editingMemoryId.value !== null) await memories.update(editingMemoryId.value, payload)
    else await memories.create(payload)
    showForm.value = false
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function remove(id: string | number) {
  if (!confirmAction('确定删除这条全局记忆吗？')) return
  try {
    await memories.remove(id)
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

function openCandidate(candidate: ProfileCandidateRecord) {
  localError.value = ''
  selectedCandidate.value = candidate
  candidateDraft.value = JSON.stringify(candidate.proposed_changes ?? {}, null, 2)
}

async function acceptCandidate(useEdited = false) {
  const candidate = selectedCandidate.value
  if (!candidate) return
  localError.value = ''
  try {
    const changes = useEdited ? JSON.parse(candidateDraft.value) : candidate.proposed_changes
    if (!changes || typeof changes !== 'object' || Array.isArray(changes)) throw new Error('画像变更必须是 JSON 对象')
    await memories.acceptCandidate(candidate, changes as Record<string, unknown>)
    selectedCandidate.value = null
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function rejectCandidate() {
  const candidate = selectedCandidate.value
  if (!candidate || !confirmAction('确定忽略这条画像更新建议吗？相同建议不会再次提示。')) return
  try {
    await memories.rejectCandidate(candidate)
    selectedCandidate.value = null
  } catch (error) {
    localError.value = errorMessage(error)
  }
}

async function rebaseCandidate(candidate: ProfileCandidateRecord) {
  localError.value = ''
  await memories.rebaseCandidate(candidate)
}

function statusLabel(status?: string): string {
  return ({
    inferred: '自动推断', confirmed: '已确认', promoted: '已晋升', stale: '已过期', conflicted: '有冲突',
  } as Record<string, string>)[status ?? ''] ?? status ?? '-'
}

function typeLabel(type?: string): string {
  return ({ fact: '事实', procedure: '操作经验', preference: '用户偏好', observation: '临时观察' } as Record<string, string>)[type ?? ''] ?? type ?? '-'
}

function displayValue(value: unknown): string {
  if (Array.isArray(value)) return value.join('、')
  if (value && typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value ?? '-')
}

function changePage(section: KnowledgeTab, page: number) {
  void memories.goToPage(section, page)
}

onMounted(() => {
  void memories.load()
})
</script>

<template>
  <section class="page-shell">
    <div class="page-container">
      <PageHeader title="知识与记忆" description="管理跨会话知识，并审核从任务证据中发现的服务画像变更。">
        <template #actions>
          <button class="btn" type="button" :disabled="memories.loading" @click="memories.load()">刷新</button>
          <button v-if="activeTab === 'memories'" class="btn btn-primary" type="button" @click="openMemory()">新增记忆</button>
        </template>
      </PageHeader>

      <div v-if="localError || memories.error" class="notice notice-error">{{ localError || memories.error }}</div>

      <div class="knowledge-tabs">
        <button type="button" :class="{ active: activeTab === 'memories' }" @click="activeTab = 'memories'">已确认记忆 <span>{{ memories.memoryPagination.total }}</span></button>
        <button type="button" :class="{ active: activeTab === 'candidates' }" @click="activeTab = 'candidates'">画像候选 <span>{{ memories.candidatePagination.total }}</span></button>
        <button type="button" :class="{ active: activeTab === 'issues' }" @click="activeTab = 'issues'">冲突与过期 <span>{{ memories.issuePagination.total + memories.expiredCandidates.length }}</span></button>
      </div>

      <div v-if="activeTab === 'memories'" class="memory-toolbar">
        <input v-model="memories.query" class="input" placeholder="搜索主题、值或目标" @keyup.enter="memories.search()" />
        <select v-model="memories.typeFilter" class="select" @change="memories.search()">
          <option value="">全部类型</option><option value="fact">事实</option><option value="procedure">操作经验</option><option value="preference">用户偏好</option><option value="observation">临时观察</option>
        </select>
        <select v-model="memories.statusFilter" class="select" @change="memories.search()">
          <option value="">全部状态</option><option value="inferred">自动推断</option><option value="confirmed">已确认</option><option value="promoted">已晋升</option><option value="stale">已过期</option><option value="conflicted">有冲突</option>
        </select>
        <button class="btn" type="button" @click="memories.search()">搜索</button>
      </div>

      <AsyncState :loading="memories.loading" :error="memories.error" @retry="memories.load()">
        <div v-if="activeTab === 'memories'" class="panel-card">
          <div v-if="!memories.items.length" class="empty-state"><strong>暂无匹配记忆</strong><span>可以手工添加，也可以让任务完成后自动沉淀。</span></div>
          <div v-else class="data-table-wrap knowledge-table-wrap">
            <table class="data-table">
              <thead><tr><th>主题</th><th>关系</th><th>内容</th><th>类型</th><th>状态</th><th>目标</th><th>证据</th><th /></tr></thead>
              <tbody>
                <tr v-for="item in memories.items" :key="String(item.id)">
                  <td><strong>{{ item.subject || '-' }}</strong></td>
                  <td><code>{{ item.predicate || 'note' }}</code></td>
                  <td class="memory-value">{{ item.value || '-' }}</td>
                  <td><span class="badge">{{ typeLabel(item.type) }}</span></td>
                  <td><span class="badge" :class="item.status === 'conflicted' ? 'badge-danger' : item.status === 'stale' ? 'badge-warning' : 'badge-success'">{{ statusLabel(item.status) }}</span></td>
                  <td>{{ item.target || '-' }}</td>
                  <td class="evidence-cell">{{ item.evidence_summary || item.source || '-' }}<small v-if="item.source_task_id">{{ item.source_task_id }}</small></td>
                  <td><div class="inline-actions"><button class="btn btn-small" type="button" @click="openMemory(item)">编辑</button><button class="btn btn-danger btn-small" type="button" @click="remove(item.id)">删除</button></div></td>
                </tr>
              </tbody>
            </table>
          </div>
          <PaginationBar :page="memories.memoryPagination.page" :total-pages="memories.memoryPagination.totalPages" :total="memories.memoryPagination.total" @change="changePage('memories', $event)" />
        </div>

        <div v-else-if="activeTab === 'candidates'" class="candidate-grid">
          <article v-for="candidate in memories.candidates" :key="candidate.id" class="panel-card candidate-card">
            <div class="candidate-head"><div><small class="eyebrow">画像更新候选</small><h2>{{ candidate.service_name }}</h2></div><span class="badge badge-warning">待审核</span></div>
            <dl class="candidate-meta">
              <div><dt>目标服务器</dt><dd>{{ displayValue(candidate.proposed_changes?.servers ?? candidate.evidence?.target) }}</dd></div>
              <div><dt>置信度</dt><dd>{{ Math.round(Number(candidate.confidence ?? 0) * 100) }}%</dd></div>
              <div class="wide"><dt>来源任务</dt><dd><code>{{ candidate.source_task_id || '-' }}</code></dd></div>
              <div class="wide"><dt>证据</dt><dd>{{ candidate.evidence?.summary || '-' }}</dd></div>
            </dl>
            <div class="candidate-fields"><span v-for="(_, key) in candidate.proposed_changes" :key="key" class="badge badge-accent">{{ key }}</span></div>
            <div class="card-actions"><button class="btn btn-primary btn-small" type="button" @click="openCandidate(candidate)">审核详情</button></div>
          </article>
          <div v-if="!memories.candidates.length" class="panel-card empty-state full-width"><strong>没有待审核候选</strong><span>任务发现稳定服务事实后，会在这里等待确认。</span></div>
          <PaginationBar class="pagination-full" :page="memories.candidatePagination.page" :total-pages="memories.candidatePagination.totalPages" :total="memories.candidatePagination.total" @change="changePage('candidates', $event)" />
        </div>

        <div v-else class="issue-stack">
          <section v-if="memories.expiredCandidates.length" class="panel-card">
            <div class="section-heading"><div><h2>已过期画像候选</h2><p>画像版本已变化，需基于最新版重新检查后才能写入。</p></div></div>
            <div class="data-table-wrap">
              <table class="data-table expired-candidate-table">
                <thead><tr><th>服务</th><th>目标服务器</th><th>建议字段</th><th>来源任务</th><th>状态</th><th /></tr></thead>
                <tbody>
                  <tr v-for="candidate in memories.expiredCandidates" :key="candidate.id">
                    <td><strong>{{ candidate.service_name }}</strong><template v-if="candidate.service_id && candidate.service_id.toLowerCase() !== candidate.service_name.toLowerCase()"><br><code>{{ candidate.service_id }}</code></template></td>
                    <td>{{ displayValue(candidate.proposed_changes?.servers ?? candidate.evidence?.target) }}</td>
                    <td><span v-for="(_, key) in candidate.proposed_changes" :key="key" class="badge badge-accent field-badge">{{ key }}</span></td>
                    <td><code>{{ candidate.source_task_id || '-' }}</code></td>
                    <td><span class="badge badge-warning">已过期</span></td>
                    <td><button class="btn btn-small expired-action" type="button" title="基于最新版重新合并" :disabled="memories.saving" @click="rebaseCandidate(candidate)">重新合并</button></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section v-if="memories.issues.length || !memories.expiredCandidates.length" class="panel-card">
            <div v-if="!memories.issues.length" class="empty-state"><strong>没有冲突或过期知识</strong><span>当前长期知识状态正常。</span></div>
            <div v-else class="data-table-wrap knowledge-table-wrap">
              <table class="data-table">
                <thead><tr><th>主题</th><th>关系</th><th>内容</th><th>问题</th><th>目标</th><th>最后观察</th><th /></tr></thead>
                <tbody>
                  <tr v-for="item in memories.issues" :key="String(item.id)">
                    <td><strong>{{ item.subject }}</strong></td><td><code>{{ item.predicate }}</code></td><td class="memory-value">{{ item.value }}</td>
                    <td><span class="badge" :class="item.status === 'conflicted' ? 'badge-danger' : 'badge-warning'">{{ statusLabel(item.status) }}</span></td>
                    <td>{{ item.target || '-' }}</td><td>{{ item.observed_at || item.updated_at || '-' }}</td>
                    <td><button class="btn btn-small" type="button" @click="openMemory(item)">处理</button></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <PaginationBar :page="memories.issuePagination.page" :total-pages="memories.issuePagination.totalPages" :total="memories.issuePagination.total" @change="changePage('issues', $event)" />
          </section>
        </div>
      </AsyncState>
    </div>

    <div v-if="showForm" class="dialog-backdrop" @click.self="showForm = false">
      <form class="dialog memory-dialog" @submit.prevent="save">
        <div class="dialog-header"><h2>{{ editingMemoryId === null ? '新增全局记忆' : '编辑全局记忆' }}</h2><button class="btn btn-ghost btn-small" type="button" @click="showForm = false">关闭</button></div>
        <div class="dialog-body form-grid">
          <div class="field"><label for="memory-subject">主题</label><input id="memory-subject" v-model="form.subject" class="input" required /></div>
          <div class="field"><label for="memory-predicate">关系</label><input id="memory-predicate" v-model="form.predicate" class="input" /></div>
          <div class="field"><label for="memory-type">类型</label><select id="memory-type" v-model="form.type" class="select"><option value="fact">事实</option><option value="procedure">操作经验</option><option value="preference">用户偏好</option><option value="observation">临时观察</option></select></div>
          <div class="field"><label for="memory-status">状态</label><select id="memory-status" v-model="form.status" class="select"><option value="inferred">自动推断</option><option value="confirmed">已确认</option><option value="promoted">已晋升</option><option value="stale">已过期</option><option value="conflicted">有冲突</option></select></div>
          <div class="field span-2"><label for="memory-value">内容</label><textarea id="memory-value" v-model="form.value" class="textarea" required /></div>
          <div class="field"><label for="memory-target">目标服务器</label><input id="memory-target" v-model="form.target" class="input" placeholder="例如 dev-01" /></div>
          <div class="field"><label for="memory-confidence">置信度</label><input id="memory-confidence" v-model.number="form.confidence" class="input" type="number" min="0" max="1" step="0.05" /></div>
          <div class="field span-2"><label for="memory-expires">失效时间</label><input id="memory-expires" v-model="form.expires_at" class="input" placeholder="YYYY-MM-DDTHH:mm:ss，留空表示长期有效" /></div>
          <div class="field span-2"><label for="memory-evidence">证据摘要</label><textarea id="memory-evidence" v-model="form.evidence_summary" class="textarea" /></div>
        </div>
        <div class="dialog-footer"><button class="btn" type="button" @click="showForm = false">取消</button><button class="btn btn-primary" type="submit" :disabled="memories.saving">{{ memories.saving ? '保存中…' : '保存' }}</button></div>
      </form>
    </div>

    <div v-if="selectedCandidate" class="dialog-backdrop" @click.self="selectedCandidate = null">
      <div class="dialog candidate-dialog">
        <div class="dialog-header"><div><small class="eyebrow">服务画像审核</small><h2>{{ selectedCandidate.service_name }}</h2></div><button class="btn btn-ghost btn-small" type="button" @click="selectedCandidate = null">关闭</button></div>
        <div class="dialog-body candidate-review">
          <div class="notice notice-info">目标服务器：{{ displayValue(selectedCandidate.proposed_changes?.servers ?? selectedCandidate.evidence?.target) }} · 来源任务：{{ selectedCandidate.source_task_id || '-' }}</div>
          <table class="data-table diff-table"><thead><tr><th>字段</th><th>当前值</th><th>建议值</th></tr></thead><tbody><tr v-for="([key, value]) in candidateChanges" :key="key"><td><code>{{ key }}</code></td><td><pre>{{ displayValue(selectedCandidate.before_snapshot?.[key]) }}</pre></td><td><pre>{{ displayValue(value) }}</pre></td></tr></tbody></table>
          <div class="field"><label for="candidate-json">编辑建议 JSON</label><textarea id="candidate-json" v-model="candidateDraft" class="textarea code-editor" spellcheck="false" /></div>
          <div class="evidence-panel"><strong>证据</strong><p>{{ selectedCandidate.evidence?.summary || '无证据摘要' }}</p><small>置信度 {{ Math.round(Number(selectedCandidate.confidence ?? 0) * 100) }}%</small></div>
        </div>
        <div class="dialog-footer"><button class="btn btn-danger" type="button" @click="rejectCandidate">忽略</button><span class="dialog-spacer" /><button class="btn" type="button" :disabled="memories.saving" @click="acceptCandidate(false)">按原建议写入</button><button class="btn btn-primary" type="button" :disabled="memories.saving" @click="acceptCandidate(true)">按编辑内容写入</button></div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.knowledge-tabs { display: flex; gap: 5px; margin-bottom: 12px; border-bottom: 1px solid var(--border); }
.knowledge-tabs button { display: flex; align-items: center; gap: 7px; padding: 9px 12px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-secondary); }
.knowledge-tabs button.active { border-bottom-color: var(--accent); color: var(--text-primary); }
.knowledge-tabs span { padding: 1px 5px; border-radius: 999px; background: var(--bg-tertiary); color: var(--text-muted); font-size: 10px; }
.memory-toolbar { display: grid; grid-template-columns: minmax(260px,1fr) 150px 150px auto; gap: 8px; margin-bottom: 12px; }
.knowledge-table-wrap { max-height: calc(100vh - 245px); }
.memory-value { min-width: 220px; max-width: 460px; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.5; }
.evidence-cell { min-width: 180px; max-width: 320px; color: var(--text-secondary); }
.evidence-cell small { display: block; margin-top: 4px; color: var(--text-muted); }
.candidate-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(330px,1fr)); gap: 12px; }
.candidate-card { padding: 14px; }
.candidate-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.candidate-head h2 { margin: 3px 0 0; font-size: 15px; }
.eyebrow { color: var(--text-muted); font-size: 10px; text-transform: uppercase; }
.candidate-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin: 14px 0; }
.candidate-meta .wide { grid-column: span 2; }
.candidate-meta dt { color: var(--text-muted); font-size: 10px; }
.candidate-meta dd { margin: 3px 0 0; overflow-wrap: anywhere; color: var(--text-secondary); }
.candidate-fields { display: flex; flex-wrap: wrap; gap: 5px; min-height: 22px; }
.card-actions { display: flex; justify-content: flex-end; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); }
.full-width { grid-column: 1 / -1; }
.pagination-full { grid-column: 1 / -1; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-secondary); }
.issue-stack { display: grid; gap: 12px; }
.section-heading h2 { margin: 0; font-size: 15px; }
.section-heading p { margin: 4px 0 0; color: var(--text-muted); font-size: 12px; }
.field-badge { margin: 2px 4px 2px 0; }
.expired-candidate-table th:nth-child(2), .expired-candidate-table td:nth-child(2) { min-width: 88px; }
.expired-action { white-space: nowrap; }
.memory-dialog { width: min(760px,100%); }
.candidate-dialog { width: min(900px,100%); }
.candidate-review { display: grid; gap: 14px; }
.diff-table pre { max-width: 330px; margin: 0; overflow: auto; white-space: pre-wrap; color: var(--text-secondary); }
.code-editor { min-height: 210px; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: 11px; }
.evidence-panel { padding: 11px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); }
.evidence-panel p { margin: 7px 0; color: var(--text-secondary); }
.evidence-panel small { color: var(--text-muted); }
.dialog-spacer { flex: 1; }
@media(max-width:760px){.memory-toolbar{grid-template-columns:1fr}.candidate-grid{grid-template-columns:1fr}.candidate-meta{grid-template-columns:1fr}.candidate-meta .wide{grid-column:auto}}
</style>
