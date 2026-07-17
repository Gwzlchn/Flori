export type DocumentQualityStatus = 'complete' | 'degraded' | 'rejected'

export interface DocumentHtmlLocator {
  dom_path: string
  exact?: string | null
}

export interface DocumentPdfLocator {
  page: number
  bboxes?: [number, number, number, number][]
  ocr_confidence?: number | null
}

export interface DocumentSourceLocator {
  source_fingerprint: string
  html?: DocumentHtmlLocator | null
  pdf?: DocumentPdfLocator | null
}

export interface DocumentExtraction {
  method?: string
  confidence?: number | null
  status?: DocumentQualityStatus
  reasons?: string[]
}

export interface DocumentFigureMedia {
  media_id: string
  role?: string | null
  artifact?: string | null
  alt?: string | null
  width?: number | null
  height?: number | null
}

export interface DocumentFigure {
  figure_id: string
  label: string
  caption: string
  source_locator: DocumentSourceLocator
  order?: number
  media?: DocumentFigureMedia[]
  extraction?: DocumentExtraction
}

export type DocumentTableCellRole = 'column_header' | 'row_header' | 'data'

export interface DocumentTableCell {
  cell_id: string
  row: number
  col: number
  rowspan?: number
  colspan?: number
  role?: DocumentTableCellRole
  text: string
}

export interface DocumentTableRepresentation {
  kind: 'structured' | 'source_crop'
  artifact?: string | null
}

export interface DocumentTable {
  table_id: string
  label: string
  caption: string
  source_locator: DocumentSourceLocator
  order?: number
  cells?: DocumentTableCell[]
  representations?: DocumentTableRepresentation[]
  footnotes?: string[]
  extraction?: DocumentExtraction
}

export interface DocumentQualityReport {
  status: DocumentQualityStatus
  reasons: string[]
  metrics?: Record<string, unknown>
}

export interface VisualCatalogItem {
  id: string
  kind: 'figure' | 'table'
  label: string
  caption: string
  order: number
}

export type AssetUrlResolver = (artifact: string) => string
export type SourceUrlResolver = (visualId: string) => string | null

export function extractionStatus(item: { extraction?: DocumentExtraction }): DocumentQualityStatus {
  return item.extraction?.status ?? 'complete'
}

export function extractionReasons(item: { extraction?: DocumentExtraction }): string[] {
  return item.extraction?.reasons ?? []
}
