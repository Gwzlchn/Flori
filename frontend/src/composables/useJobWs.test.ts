import { describe, expect, it } from 'vitest'

import { applyJobStepEvent } from './useJobWs'
import type { JobPartInfo, StepInfo } from '../types'

function step(name: string, status = 'waiting'): StepInfo {
  return {
    name, label: name, status, started_at: null, finished_at: null,
    duration_sec: null, meta: {}, error: null, worker_id: null,
  }
}

describe('useJobWs 多Part执行事件', () => {
  it('encoded step只更新目标Part并重算Part聚合状态', () => {
    const rootSteps = [step('09_merge_parts')]
    const parts: JobPartInfo[] = [
      {
        part_id: 'pt_a', part_index: 1, title: null, url: null,
        status: 'pending', progress_pct: 0, media: {},
        steps: [step('01_download'), step('08_punctuate')],
      },
      {
        part_id: 'pt_b', part_index: 2, title: null, url: null,
        status: 'pending', progress_pct: 0, media: {},
        steps: [step('01_download'), step('08_punctuate')],
      },
    ]

    applyJobStepEvent(rootSteps, parts, {
      event: 'step_done', step: 'part:pt_a::01_download', duration_sec: 3,
    })
    expect(parts[0].steps[0].status).toBe('done')
    expect(parts[0].progress_pct).toBe(50)
    expect(parts[1].steps[0].status).toBe('waiting')
    expect(rootSteps[0].status).toBe('waiting')

    applyJobStepEvent(rootSteps, parts, {
      event: 'step_failed', step: 'part:pt_a::08_punctuate', error: 'boom',
    })
    expect(parts[0].status).toBe('failed')
    expect(parts[0].steps[1].error).toBe('boom')
  })
})
