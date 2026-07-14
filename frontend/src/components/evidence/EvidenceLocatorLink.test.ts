import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { CanonicalEvidenceProjection } from '../../types'
import EvidenceLocatorLink from './EvidenceLocatorLink.vue'

function evidence(status: CanonicalEvidenceProjection['status'], over: Partial<CanonicalEvidenceProjection> = {}): CanonicalEvidenceProjection {
  return {
    evidence_id: `ce_${'1'.repeat(64)}`, status, reason: status === 'valid' ? null : status,
    job_id: 'job-1', note_type: 'smart', chunk_id: 'job-1:smart:0', section: '引言',
    evidence_fingerprint: 'a'.repeat(64), source_fingerprint: 'b'.repeat(64),
    locator: status === 'valid' ? { kind: 'pdf', page: 3, bbox: null } : null,
    link: status === 'valid' ? { kind: 'pdf', href: `/api/evidence/ce_${'1'.repeat(64)}/open`, label: '第 3 页' } : null,
    validated_at: '2026-07-14T14:00:00Z', ...over,
  }
}

describe('EvidenceLocatorLink', () => {
  it('valid 且有服务端 link 时才可点击', () => {
    const w = mount(EvidenceLocatorLink, { props: { evidence: evidence('valid') } })
    expect(w.get('a').attributes('href')).toBe(`/api/evidence/ce_${'1'.repeat(64)}/open`)
    expect(w.text()).toContain('第 3 页')
  })

  it.each([
    ['stale', '证据已过期'],
    ['missing', '证据缺失'],
  ] as const)('%s 明确不可跳转', (status, label) => {
    const w = mount(EvidenceLocatorLink, { props: { evidence: evidence(status) } })
    expect(w.find('a').exists()).toBe(false)
    expect(w.text()).toContain(label)
  })

  it('即使状态 valid 也拒绝跨站或路径注入', () => {
    for (const href of ['https://evil.example/x', '//evil.example/x', '/ok\\..\\secret', '/safe/%2e%2e/secret', '/safe/%252e%252e/secret']) {
      const w = mount(EvidenceLocatorLink, {
        props: { evidence: evidence('valid', { link: { kind: 'text', href, label: '伪造链接' } }) },
      })
      expect(w.find('a').exists()).toBe(false)
    }
  })

  it('拒绝伪造 ID、非空失效原因和 locator/link 类型不一致', () => {
    const variants = [
      evidence('valid', { evidence_id: 'ce_bad' }),
      evidence('valid', { reason: 'unexpected' }),
      evidence('valid', { link: { kind: 'media', href: '/jobs/job-1', label: '伪造类型' } }),
    ]
    for (const item of variants) {
      const w = mount(EvidenceLocatorLink, { props: { evidence: item } })
      expect(w.find('a').exists()).toBe(false)
    }
  })

  it('不会从 locator 的任何字段自行拼接链接', () => {
    const w = mount(EvidenceLocatorLink, {
      props: { evidence: evidence('valid', { locator: { kind: 'text', exact: '../../secret', prefix: null, suffix: null, dom_path: null }, link: null }) },
    })
    expect(w.find('a').exists()).toBe(false)
    expect(w.html()).not.toContain('secret')
  })
})
