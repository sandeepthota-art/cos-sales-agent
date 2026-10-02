import { apiGet, apiPatch, buildQuery } from './client'
import type { PageParams, ProjectFieldsUpdate, ProjectRow, ProjectSummary } from './types'

export function listProjects(params: PageParams = {}): Promise<ProjectRow[]> {
  return apiGet(`/projects${buildQuery(params)}`)
}

export function getProject(projectId: string): Promise<ProjectSummary> {
  return apiGet(`/projects/${encodeURIComponent(projectId)}`)
}

export function updateProject(projectId: string, fields: ProjectFieldsUpdate): Promise<ProjectRow> {
  return apiPatch(`/projects/${encodeURIComponent(projectId)}`, fields)
}
