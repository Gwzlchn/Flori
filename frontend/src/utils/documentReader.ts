export function boundDocumentImageUrl(jobId: string, source: string, baseUrl: string): string | null {
  let url: URL
  let base: URL
  try {
    url = new URL(source, baseUrl)
    base = new URL(baseUrl)
  } catch {
    return null
  }
  const expectedPath = `/api/jobs/${encodeURIComponent(jobId)}/document/resource`
  const paths = url.searchParams.getAll('path')
  if (url.origin !== base.origin || url.pathname !== expectedPath || paths.length !== 1 || !paths[0]) return null
  return url.href
}
