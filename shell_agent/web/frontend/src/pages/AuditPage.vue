<script setup lang="ts">
import { onMounted } from 'vue'

import AsyncState from '../components/common/AsyncState.vue'
import PageHeader from '../components/common/PageHeader.vue'
import { useAuditStore } from '../stores/audit'

const audit = useAuditStore()

function resultClass(executed?: boolean, exitCode?: number | null): string {
  if (!executed) return 'badge-warning'
  return exitCode === 0 ? 'badge-success' : 'badge-danger'
}

onMounted(() => {
  void audit.load()
})
</script>

<template>
  <section class="page-shell">
    <div class="page-container">
      <PageHeader title="审计日志" description="查看命令预览、确认与实际执行留下的审计记录。">
        <template #actions>
          <input v-model="audit.target" class="input audit-filter" placeholder="按目标过滤" @keyup.enter="audit.load()" />
          <button class="btn" type="button" :disabled="audit.loading" @click="audit.load()">刷新</button>
        </template>
      </PageHeader>

      <div class="panel-card">
        <AsyncState
          :loading="audit.loading"
          :error="audit.error"
          :empty="audit.records.length === 0"
          empty-title="暂无审计记录"
          empty-description="执行或预览命令后，记录会显示在这里。"
          @retry="audit.load()"
        >
          <div class="data-table-wrap audit-table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>目标</th>
                  <th>命令</th>
                  <th>结果</th>
                  <th>来源</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(record, index) in audit.records" :key="String(record.id ?? index)">
                  <td class="mono nowrap">{{ record.timestamp ?? record.created_at ?? '-' }}</td>
                  <td>
                    <div>{{ record.target || '-' }}</div>
                    <small class="text-muted">{{ record.target_env || record.executor || '' }}</small>
                  </td>
                  <td><code class="command-cell">{{ record.command || '-' }}</code></td>
                  <td>
                    <span class="badge" :class="resultClass(record.executed, record.exit_code)">
                      {{ !record.executed ? '未执行' : record.exit_code === 0 ? '成功' : `退出 ${record.exit_code ?? '-'}` }}
                    </span>
                  </td>
                  <td>{{ record.source || '-' }}</td>
                  <td class="nowrap">{{ record.duration_ms == null ? '-' : `${record.duration_ms} ms` }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </AsyncState>
      </div>
    </div>
  </section>
</template>

<style scoped>
.audit-filter { width: 190px; }
.audit-table-wrap { max-height: calc(100vh - 190px); }
.command-cell { display: block; min-width: 260px; max-width: 640px; white-space: pre-wrap; overflow-wrap: anywhere; }
.nowrap { white-space: nowrap; }
</style>
