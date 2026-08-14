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

  it('联合证据只保留一个组语义并展示全部可核对来源', () => {
    const item = evidence('valid', {
      source_group_fingerprint: 'c'.repeat(64),
      sources: [
        {
          ordinal: 0,
          source_ref: 'document:body', source_segment_id: 'S1.P1', source_fingerprint: 'd'.repeat(64),
          locator: { kind: 'pdf', page: 3, bbox: null },
          link: { kind: 'pdf', href: '/content/job-1?tab=notes&view=pdf&page=3', label: '跳到 PDF 证据' },
        },
        {
          ordinal: 1,
          source_ref: 'document:body', source_segment_id: 'S1.P2', source_fingerprint: 'e'.repeat(64),
          locator: { kind: 'text', exact: 'support', prefix: '', suffix: '', dom_path: null },
          link: { kind: 'text', href: '/content/job-1?tab=notes&view=source&segment=S1.P2', label: '跳到原文证据' },
        },
      ],
      locator: null,
      link: null,
    })
    const w = mount(EvidenceLocatorLink, { props: { evidence: item } })

    expect(w.findAll('.evidence-locator-group')).toHaveLength(1)
    expect(w.findAll('.evidence-locator')).toHaveLength(0)
    expect(w.get('.evidence-group-label').text()).toBe('联合证据 2 项')
    const links = w.findAll('a.evidence-source-link')
    expect(links).toHaveLength(2)
    expect(links.map(link => link.attributes('href'))).toEqual([
      '/content/job-1?tab=notes&view=pdf&page=3',
      '/content/job-1?tab=notes&view=source&segment=S1.P2',
    ])
    expect(links.map(link => link.attributes('data-source-segment-id'))).toEqual(['S1.P1', 'S1.P2'])
  })

  it('联合证据任一成员链接异常时整组拒绝展示', () => {
    const item = evidence('valid', {
      sources: [
        {
          ordinal: 0,
          source_ref: 'document:body', source_segment_id: 'S1.P1', source_fingerprint: 'd'.repeat(64),
          locator: { kind: 'pdf', page: 3, bbox: null },
          link: { kind: 'pdf', href: '/content/job-1?page=3', label: '跳到 PDF 证据' },
        },
        {
          ordinal: 1,
          source_ref: 'document:body', source_segment_id: 'S1.P2', source_fingerprint: 'e'.repeat(64),
          locator: { kind: 'text', exact: 'support', prefix: '', suffix: '', dom_path: null },
          link: { kind: 'text', href: 'https://evil.example/source', label: '伪造来源' },
        },
      ],
      locator: null,
      link: null,
    })
    const w = mount(EvidenceLocatorLink, { props: { evidence: item } })

    expect(w.find('a').exists()).toBe(false)
    expect(w.text()).toContain('定位不可用')
  })

  it('联合证据 ordinal 与数组顺序不一致时不回退到顶层单来源链接', () => {
    const item = evidence('valid', {
      sources: [
        {
          ordinal: 1,
          source_ref: 'document:body', source_segment_id: 'S1.P1', source_fingerprint: 'd'.repeat(64),
          locator: { kind: 'pdf', page: 3, bbox: null },
          link: { kind: 'pdf', href: '/content/job-1?page=3', label: '来源 1' },
        },
        {
          ordinal: 0,
          source_ref: 'document:body', source_segment_id: 'S1.P2', source_fingerprint: 'e'.repeat(64),
          locator: { kind: 'pdf', page: 4, bbox: null },
          link: { kind: 'pdf', href: '/content/job-1?page=4', label: '来源 2' },
        },
      ],
    })
    const w = mount(EvidenceLocatorLink, { props: { evidence: item } })

    expect(w.find('a').exists()).toBe(false)
    expect(w.text()).toContain('定位不可用')
  })
})
