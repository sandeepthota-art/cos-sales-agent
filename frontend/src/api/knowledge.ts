import { apiGet, buildQuery } from './client'
import type { KnowledgeItemRow, PageParams } from './types'

export function listKnowledge(
  params: PageParams & { threadId?: string } = {},
): Promise<KnowledgeItemRow[]> {
  const { threadId, ...rest } = params
  return apiGet(`/knowledge${buildQuery({ ...rest, thread_id: threadId })}`)
}
