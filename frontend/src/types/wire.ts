/** Selected HTTP JSON wire aliases. Generated declarations are never edited by hand. */
import type { components } from './generated/api'

type Schema = components['schemas']

export type ErrorWire = Schema['ErrorResponse']
export type SourceCatalogWire = Schema['SourceCatalogResponse']
export type JobCreatedWire = Schema['JobCreatedResponse']
export type JobListWire = Schema['JobListResponse']
export type JobDetailWire = Omit<Schema['JobDetailResponse'], 'meta'> & {
  meta: Record<string, unknown>
}
export type WorkerWire = Omit<Schema['WorkerResponse'], 'spec' | 'load' | 'traffic'> & {
  spec?: Record<string, unknown>
  load?: Record<string, unknown>
  traffic?: Record<string, unknown>
}
export type FullStatusWire = Schema['FullStatusResponse']
export type StudyCardWire = Schema['StudyCardResponse']
export type StudySuggestionWire = Schema['StudySuggestionResponse']
export type ReviewWire = Schema['ReviewProjectionResponse']
export type EvidenceWire = Schema['EvidenceProjectionResponse']
export type CanonicalEvidenceWire = Schema['CanonicalEvidenceProjection']
export type ConceptDetailWire = Schema['ConceptTermDetailResponse']
export type SearchWire = Schema['SearchResponse']
export type AskWire = Schema['AskResponse']
export type AiTaskResultWire = Schema['AiTaskResultResponse']
export type PromptDetailWire = Schema['PromptDetailResponse']
export type PromptVersionWire = Schema['PromptVersionResponse']

// WebSocket、文本/二进制/Range 和开放审计 payload 不由 generated client 接管。
export type OpenWireObject = Record<string, unknown>
