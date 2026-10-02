import { apiGet, apiPatch, buildQuery } from './client'
import type { OpportunityFieldsUpdate, OpportunityRow, PageParams } from './types'

export function listOpportunities(params: PageParams = {}): Promise<OpportunityRow[]> {
  return apiGet(`/opportunities${buildQuery(params)}`)
}

export function getOpportunity(opportunityId: string): Promise<OpportunityRow> {
  return apiGet(`/opportunities/${encodeURIComponent(opportunityId)}`)
}

export function updateOpportunity(
  opportunityId: string,
  fields: OpportunityFieldsUpdate,
): Promise<OpportunityRow> {
  return apiPatch(`/opportunities/${encodeURIComponent(opportunityId)}`, fields)
}
