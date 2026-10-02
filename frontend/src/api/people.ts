import { apiGet, buildQuery } from './client'
import type { PageParams, PersonContext, PersonRow } from './types'

export function listPeople(params: PageParams = {}): Promise<PersonRow[]> {
  return apiGet(`/people${buildQuery(params)}`)
}

export function getPerson(personId: string): Promise<PersonContext> {
  return apiGet(`/people/${encodeURIComponent(personId)}`)
}
