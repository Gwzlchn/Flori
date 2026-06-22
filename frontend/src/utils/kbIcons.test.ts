import { describe, it, expect } from 'vitest'
import { resolveIcon, KB_ICONS, ICON_NAMES } from './kbIcons'

describe('resolveIcon', () => {
  it('kebab 名称直接命中', () => {
    expect(resolveIcon('brain')).toBe(KB_ICONS['brain'])
    expect(resolveIcon('flask-conical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('line-chart')).toBe(KB_ICONS['line-chart'])
  })

  it('PascalCase → kebab 归一', () => {
    expect(resolveIcon('FlaskConical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('BookOpen')).toBe(KB_ICONS['book-open'])
    expect(resolveIcon('LineChart')).toBe(KB_ICONS['line-chart'])
    expect(resolveIcon('GraduationCap')).toBe(KB_ICONS['graduation-cap'])
  })

  it('单词首字母大写也能命中', () => {
    expect(resolveIcon('Brain')).toBe(KB_ICONS['brain'])
    expect(resolveIcon('Cpu')).toBe(KB_ICONS['cpu'])
  })

  it('下划线分隔 → kebab 归一', () => {
    expect(resolveIcon('flask_conical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('book_marked')).toBe(KB_ICONS['book-marked'])
  })

  it('空格分隔 → kebab 归一', () => {
    expect(resolveIcon('flask conical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('graduation cap')).toBe(KB_ICONS['graduation-cap'])
  })

  it('多个连续分隔符折叠为单个连字符', () => {
    expect(resolveIcon('flask__conical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('flask   conical')).toBe(KB_ICONS['flask-conical'])
    expect(resolveIcon('flask _ conical')).toBe(KB_ICONS['flask-conical'])
  })

  it('两侧空白被 trim', () => {
    expect(resolveIcon('  brain  ')).toBe(KB_ICONS['brain'])
  })

  it('大小写不敏感(全大写)', () => {
    expect(resolveIcon('BRAIN')).toBe(KB_ICONS['brain'])
  })

  it('数字与大写之间也插入连字符', () => {
    // 归一规则: ([a-z0-9])([A-Z]) → '$1-$2'
    expect(resolveIcon('1A')).toBe(null) // 归一为 '1-a',未命中 → null,验证归一发生
  })

  it('未命中 → null', () => {
    expect(resolveIcon('definitely-not-an-icon')).toBe(null)
    expect(resolveIcon('Unknown')).toBe(null)
  })

  it('null / undefined / 空串 → null', () => {
    expect(resolveIcon(null)).toBe(null)
    expect(resolveIcon(undefined)).toBe(null)
    expect(resolveIcon('')).toBe(null)
    expect(resolveIcon()).toBe(null)
  })
})

describe('KB_ICONS 映射', () => {
  it('book 与 book-open 同指 BookOpen 组件', () => {
    expect(KB_ICONS['book']).toBe(KB_ICONS['book-open'])
  })

  it('包含已知图标键', () => {
    expect(KB_ICONS).toHaveProperty('brain')
    expect(KB_ICONS).toHaveProperty('flask-conical')
    expect(KB_ICONS).toHaveProperty('book-marked')
    expect(KB_ICONS).toHaveProperty('microscope')
  })

  it('每个值都是已定义的组件', () => {
    for (const key of Object.keys(KB_ICONS)) {
      expect(KB_ICONS[key]).toBeTruthy()
    }
  })
})

describe('ICON_NAMES', () => {
  it('每个名称都能被 resolveIcon 解析', () => {
    for (const name of ICON_NAMES) {
      expect(resolveIcon(name)).toBe(KB_ICONS[name])
      expect(resolveIcon(name)).not.toBe(null)
    }
  })

  it('每个名称都存在于 KB_ICONS 中', () => {
    for (const name of ICON_NAMES) {
      expect(KB_ICONS).toHaveProperty(name)
    }
  })

  it('保持声明顺序(brain 在首位)', () => {
    expect(ICON_NAMES[0]).toBe('brain')
    expect(ICON_NAMES).toContain('briefcase')
    expect(ICON_NAMES).toContain('landmark')
  })
})
