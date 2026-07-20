import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn(), upload: vi.fn(), getText: vi.fn() }
const push = vi.fn()
const toast = vi.fn()
vi.mock('../composables/useApi', () => ({ useApi: () => api }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

import RecoverySettingsView from './RecoverySettingsView.vue'

function snapshot(over: Record<string, any> = {}) {
  return {
    digest: `sha256:${'a'.repeat(64)}`,
    refs: ['latest'],
    created_at: '2026-07-19T11:00:00Z',
    source_app_version: '2.3.0',
    partial: false,
    portable_ready: true,
    readiness_reasons: [],
    completeness: {
      terminal_steps: 8, manifests_seen: 8, manifests_missing: 0, manifests_excluded: 0,
      ai_config_complete: true, user_config_complete: true, secret_scan_complete: true,
      media_self_contained: true, external_media_roots: [], portable_ready: true,
      readiness_reasons: [],
    },
    stats: { jobs: 2, parts: 3, vendored_source_parts: 3 },
    ...over,
  }
}

function recoveryStatus(over: Record<string, any> = {}) {
  const latest = snapshot()
  return {
    state: 'ready', repository_path: '/content-repo',
    host_repository_env: 'FLORI_CONTENT_REPOSITORY_DIR', write_lock: null,
    latest, snapshots: [latest], media_vendoring_available: true,
    deployment_id_configured: true, online_restore_supported: false,
    exact_dr: {
      configured: true, output_path: '/exact-dr', state: 'idle', operation: null,
      confirmation: '创建完整灾备', drain_timeout_sec: 3600,
    },
    operations: [], error: null, ...over,
  }
}

function mountView() {
  return mount(RecoverySettingsView, {
    global: { provide: { showToast: toast } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
})

describe('RecoverySettingsView', () => {
  it('展示仓库、有效产物统计与原视频闭包', async () => {
    api.get.mockResolvedValue(recoveryStatus())
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(api.get).toHaveBeenCalledWith('/api/recovery')
    expect(text).toContain('便携内容仓库')
    expect(text).toContain('可恢复')
    expect(text).toContain('2 / 3')
    expect(text).toContain('已收入CAS')
    expect(text).toContain('失败步骤仅保留元信息')
    expect(text).toContain('第二份物理备份')
    expect(wrapper.find('.recovery-state-ready').exists()).toBe(true)
    expect(wrapper.find('.state-ready').exists()).toBe(false)
  })

  it('不完整快照明确显示原因且不能生成恢复交接', async () => {
    const incomplete = snapshot({
      portable_ready: false,
      readiness_reasons: ['external_media_dependencies'],
      completeness: {
        ...snapshot().completeness,
        portable_ready: false,
        media_self_contained: false,
        external_media_roots: ['library'],
        readiness_reasons: ['external_media_dependencies'],
      },
    })
    api.get.mockResolvedValue(recoveryStatus({
      state: 'incomplete', latest: incomplete, snapshots: [incomplete],
    }))
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('external_media_dependencies')
    expect(wrapper.text()).toContain('默认仍引用NAS原目录')
    const button = wrapper.findAll('button').find(item => item.text().includes('生成恢复交接'))!
    expect(button.attributes('disabled')).toBeDefined()
  })

  it('创建备份只发送服务端选项并进入轮询状态', async () => {
    api.get.mockResolvedValue(recoveryStatus())
    api.post.mockResolvedValue({ operation: { id: 'backup-1' } })
    const wrapper = mountView()
    await flushPromises()
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    await checkboxes[0].setValue(true)
    const button = wrapper.findAll('button').find(item => item.text().includes('创建增量备份'))!
    await button.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/recovery/backups', {
      vendor_media: true,
      full_rehash: false,
    })
    expect(toast).toHaveBeenCalledWith('备份已开始', 'success')
    wrapper.unmount()
  })

  it('风险确认后生成幂等离线交接并复制命令', async () => {
    const digest = `sha256:${'a'.repeat(64)}`
    api.get.mockResolvedValue(recoveryStatus())
    api.post.mockResolvedValue({
      id: 'restore-abc', snapshot_digest: digest, plan_digest: `sha256:${'b'.repeat(64)}`,
      target_generation: 'gen-abc',
      deployment_id: 'flori-test', generated_at: '2026-07-19T11:00:00Z',
      counts: { insert: 9 }, bytes_to_write: 1024, required_source_roots: [],
      commands: { verify: 'verify cmd', exact_dr: 'dr cmd', plan: 'plan cmd', restore: 'restore cmd' },
      reused: false,
    })
    const wrapper = mountView()
    await flushPromises()
    const confirm = wrapper.find('input[placeholder="输入:准备还原"]')
    await confirm.setValue('准备还原')
    const button = wrapper.findAll('button').find(item => item.text().includes('生成恢复交接'))!
    await button.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/recovery/restore-plans', { snapshot_digest: digest })
    expect(wrapper.find('[data-test="restore-handoff"]').text()).toContain('restore-abc')
    expect(wrapper.find('[data-test="restore-handoff"]').text()).toContain('gen-abc')
    const copyButton = wrapper.findAll('button').find(item => item.text() === '复制')!
    await copyButton.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('verify cmd')
  })

  it('仓库写锁存在时禁用在线备份', async () => {
    api.get.mockResolvedValue(recoveryStatus({
      state: 'locked', write_lock: { owner: 'backup-old', acquired_at: '2026-07-19T10:00:00Z' },
    }))
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('不要自动破锁')
    const button = wrapper.findAll('button').find(item => item.text().includes('创建增量备份'))!
    expect(button.attributes('disabled')).toBeDefined()
  })

  it('风险确认后排空并创建exact DR且展示三件套', async () => {
    api.get.mockResolvedValue(recoveryStatus())
    api.post.mockResolvedValue({ operation: { id: 'exact-dr-1' } })
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('整机灾备 exact DR')
    expect(wrapper.text()).toContain('浏览器不传输大归档')
    const confirm = wrapper.find('input[placeholder="输入:创建完整灾备"]')
    const button = wrapper.findAll('button').find(item => item.text().includes('排空并创建 exact DR'))!
    expect(button.attributes('disabled')).toBeDefined()
    await confirm.setValue('创建完整灾备')
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith('/api/recovery/exact-dr', {
      confirmation: '创建完整灾备',
    })
    expect(toast).toHaveBeenCalledWith('已停止新写入,正在排空 Worker', 'success')
    wrapper.unmount()

    api.get.mockResolvedValue(recoveryStatus({
      exact_dr: {
        configured: true, output_path: '/exact-dr', state: 'success',
        confirmation: '创建完整灾备', drain_timeout_sec: 3600,
        operation: {
          id: 'exact-dr-success', status: 'success', created_at: '2026-07-20T00:00:00Z',
          finished_at: '2026-07-20T01:00:00Z', generation: 'g1',
          archive_name: 'flori-backup-g1.tar.gz',
          sidecar_name: 'flori-backup-g1.tar.gz.sha256',
          receipt_name: 'flori-backup-g1.json', archive_sha256: 'a'.repeat(64),
          size_bytes: 1024, drain: { holders: 0, running_steps: 0, quiet_samples: 2 },
          error: null,
        },
      },
    }))
    const completed = mountView()
    await flushPromises()
    const trio = completed.find('[data-test="exact-dr-trio"]')
    expect(trio.text()).toContain('flori-backup-g1.tar.gz')
    expect(trio.text()).toContain('flori-backup-g1.tar.gz.sha256')
    expect(trio.text()).toContain('flori-backup-g1.json')
  })
})
