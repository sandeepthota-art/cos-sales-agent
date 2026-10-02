import { apiGet, buildQuery } from './client'
import type { PageParams, PersonalItemRow } from './types'

export function listPersonalItems(params: PageParams = {}): Promise<PersonalItemRow[]> {
  return apiGet(`/personal-items${buildQuery(params)}`)
}
