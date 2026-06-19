// 知识库图标:精选集。用 named import(可 tree-shake),避免 `import * as L from 'lucide-vue-next'`
// 那种通配导入把整库(~758KB)打进包里。后端存的 icon 字符串在此解析为组件,未命中给调用方回退。
import type { Component } from 'vue'
import {
  Brain, Cpu, Coins, Atom, Dna, FlaskConical, BookOpen, GraduationCap,
  Code, Database, Globe, Network, Binary, Sigma, Cog, Microscope,
  Briefcase, LineChart, Palette, Music, Languages, Landmark, Lightbulb, BookMarked,
} from 'lucide-vue-next'

// 名称(小写 kebab) → 组件
export const KB_ICONS: Record<string, Component> = {
  brain: Brain, cpu: Cpu, coins: Coins, atom: Atom, dna: Dna,
  'flask-conical': FlaskConical, book: BookOpen, 'book-open': BookOpen,
  'graduation-cap': GraduationCap, code: Code, database: Database, globe: Globe,
  network: Network, binary: Binary, sigma: Sigma, cog: Cog, microscope: Microscope,
  briefcase: Briefcase, 'line-chart': LineChart, palette: Palette, music: Music,
  languages: Languages, landmark: Landmark, lightbulb: Lightbulb, 'book-marked': BookMarked,
}

// 知识库图标选择器的可选项(顺序即展示顺序)
export const ICON_NAMES: string[] = [
  'brain', 'cpu', 'coins', 'atom', 'dna', 'flask-conical', 'book', 'graduation-cap',
  'code', 'database', 'globe', 'network', 'briefcase', 'line-chart', 'microscope', 'landmark',
]

// 解析图标名(兼容 PascalCase / kebab / 下划线 / 空格);未命中返回 null,调用方给回退。
export function resolveIcon(name?: string | null): Component | null {
  if (!name) return null
  const key = name.trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/[_\s]+/g, '-')
    .toLowerCase()
  return KB_ICONS[key] ?? null
}
