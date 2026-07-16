const FALLBACK_LANGUAGE_NAMES: Record<string, string> = {
  zh: '中文', en: '英语', ja: '日语', ko: '韩语',
  de: '德语', fr: '法语', es: '西班牙语', pt: '葡萄牙语',
  it: '意大利语', ru: '俄语', ar: '阿拉伯语', nl: '荷兰语',
}

let displayNames: Intl.DisplayNames | null | undefined

export function languageName(code?: string | null): string {
  const normalized = (code || '').trim().toLowerCase()
  if (!normalized || normalized === 'unknown') return '未知'
  if (normalized === 'non-zh') return '其他语言'
  try {
    displayNames ??= new Intl.DisplayNames(['zh-CN'], { type: 'language' })
    return displayNames.of(normalized) || FALLBACK_LANGUAGE_NAMES[normalized] || normalized.toUpperCase()
  } catch {
    displayNames = null
    return FALLBACK_LANGUAGE_NAMES[normalized] || normalized.toUpperCase()
  }
}
