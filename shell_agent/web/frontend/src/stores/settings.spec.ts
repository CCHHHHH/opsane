import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '../api/http'
import { useSettingsStore, type SkillCandidate } from './settings'


const candidate: SkillCandidate = {
  id: 'skill_candidate_1',
  name: 'learned_uptime',
  description: '历史资源检查',
  status: 'pending',
  draft_yaml: 'name: learned_uptime\nenabled: false\n',
  evidence: { targets: ['dev-01'] },
  confidence: 0.79,
  risk_level: 'safe',
  occurrence_count: 3,
  source_task_ids: ['task-1', 'task-2', 'task-3'],
  created_at: '2026-07-22T10:00:00',
}

describe('settings Skill candidate actions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('loads pending history-derived candidates', async () => {
    const store = useSettingsStore()
    const get = vi.spyOn(http, 'get').mockResolvedValue({ candidates: [candidate] })

    await store.loadSkillCandidates()

    expect(get).toHaveBeenCalledWith('/api/skill-candidates', {
      status: 'pending', page: 1, page_size: 50,
    })
    expect(store.skillCandidates).toEqual([candidate])
  })

  it('rejects a candidate and removes it from the pending list', async () => {
    const store = useSettingsStore()
    store.skillCandidates = [candidate]
    const post = vi.spyOn(http, 'post').mockResolvedValue({ ok: true })

    await store.rejectSkillCandidate(candidate.id)

    expect(post).toHaveBeenCalledWith('/api/skill-candidates/skill_candidate_1/reject')
    expect(store.skillCandidates).toEqual([])
  })

  it('stores the non-executing candidate preview', async () => {
    const store = useSettingsStore()
    const post = vi.spyOn(http, 'post').mockResolvedValue({
      ok: true,
      candidate_id: candidate.id,
      test_input: '检查运行时间',
      params: { target: 'dev-01' },
      missing_params: [],
      steps: [{ index: 1, command: "ssh dev-01 'uptime'", risk_level: 'safe' }],
      will_execute: false,
    })

    await store.previewSkillCandidate(candidate.id)

    expect(post).toHaveBeenCalledWith('/api/skill-candidates/skill_candidate_1/preview')
    expect(store.skillCandidatePreviews[candidate.id].will_execute).toBe(false)
  })

  it('requests semantic grouping explicitly and keeps the scan bounded', async () => {
    const store = useSettingsStore()
    vi.spyOn(http, 'get').mockResolvedValue({ candidates: [] })
    const post = vi.spyOn(http, 'post').mockResolvedValue({
      ok: true,
      scanned_tasks: 8,
      repeated_groups: 1,
      exact_groups: 0,
      semantic_groups: 1,
      semantic: { status: 'completed' },
      created: [candidate],
    })

    await store.scanSkillCandidates(30, 3, true)

    expect(post).toHaveBeenCalledWith('/api/skill-candidates/scan', {
      days: 30,
      min_occurrences: 3,
      semantic: true,
    })
  })
})
