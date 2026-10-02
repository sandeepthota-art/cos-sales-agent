import { apiGet, buildQuery } from './client'
import type { ContextVersion, PageParams, ThreadDetail, ThreadRow } from './types'

export function listThreads(params: PageParams = {}): Promise<ThreadRow[]> {
  return apiGet(`/threads${buildQuery(params)}`)
}

export function getThread(threadId: string): Promise<ThreadDetail> {
  return apiGet(`/threads/${encodeURIComponent(threadId)}`)
}

export function getThreadContextVersions(threadId: string): Promise<ContextVersion[]> {
  return apiGet(`/threads/${encodeURIComponent(threadId)}/context-versions`)
}
