import { apiGet, buildQuery } from './client'
import type { FollowUpRow, PageParams } from './types'

export function listFollowUps(params: PageParams = {}): Promise<FollowUpRow[]> {
  return apiGet(`/follow-ups${buildQuery(params)}`)
}
