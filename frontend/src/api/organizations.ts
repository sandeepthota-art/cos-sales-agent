import { apiGet, buildQuery } from './client'
import type { OrganizationRow, OrganizationSummary, PageParams } from './types'

export function listOrganizations(params: PageParams = {}): Promise<OrganizationRow[]> {
  return apiGet(`/organizations${buildQuery(params)}`)
}

export function getOrganization(orgId: string): Promise<OrganizationSummary> {
  return apiGet(`/organizations/${encodeURIComponent(orgId)}`)
}
