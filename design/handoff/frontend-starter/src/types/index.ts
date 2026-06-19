// 前端共享类型 —— 与后端契约（docs/03-contracts.md / 02-domain-model.md）保持一致。
// 命名遵守 IA：知识库 KnowledgeBase ⊃ 集合 Collection ⊃ 内容 Content；概念 Concept 为知识层。

/** 内容处理状态机（对齐原型第 3 节状态色） */
export type JobStatus =
  | 'pending' // 待处理
  | 'downloading' // 下载中
  | 'processing' // 处理中
  | 'done' // 已完成
  | 'failed' // 失败

/** 内容类型 —— 决定 type-pill 配色（t-video / t-paper / t-article / t-audio） */
export type ContentType = 'video' | 'paper' | 'article' | 'audio'

/** mnemo.css badge 语义变体 */
export type BadgeVariant = 'ok' | 'info' | 'run' | 'warn' | 'bad' | 'mut' | 'brand'

/** 集合类型：订阅源 / 手动收藏 */
export type CollectionKind = 'subscription' | 'manual'

/** 概念采纳状态 */
export type ConceptStatus = 'accepted' | 'candidate'

/** 知识库（顶层单元） */
export interface KnowledgeBase {
  id: string
  name: string
  /** lucide 图标名，如 'cpu' / 'atom' / 'dna' */
  icon: string
  /** 卡片图标块渐变背景，直接写 CSS（如 linear-gradient(...)） */
  gradient: string
  /** nb-dot 点色 */
  dotColor: string
  collectionCount: number
  contentCount: number
  conceptCount: number
  subscriptionCount?: number
  /** 活跃文案，如 '12 分钟前活跃' */
  activeText: string
  /** 是否近期活跃（控制 d-ok / d-mut 点色） */
  active: boolean
}

/** 集合 */
export interface Collection {
  id: string
  name: string
  kind: CollectionKind
  knowledgeBaseId: string
  knowledgeBaseName: string
  contentCount: number
  tags: string[]
  description?: string
}

/** 内容（每条投递） */
export interface Content {
  id: string
  title: string
  type: ContentType
  status: JobStatus
  /** 来源平台，如 'Bilibili' / 'arXiv' / '公众号' */
  source?: string
  knowledgeBaseName?: string
  collectionName?: string
  /** 处理中时的当前步骤，如 '08_智能笔记' */
  currentStep?: string
  /** 0-100 进度 */
  progress?: number
  /** 质量评分 */
  rating?: number
  /** 时长 / 相对时间等右侧元信息 */
  metaText?: string
}

/** 概念 */
export interface Concept {
  id: string
  name: string
  definition: string
  status: ConceptStatus
  /** 是否主题概念（pin） */
  isTopic?: boolean
  /** 佐证强度 0-5 */
  strength?: number
  /** 出现内容数 */
  occurrenceCount?: number
}
