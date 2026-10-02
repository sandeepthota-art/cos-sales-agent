import { apiGet, buildQuery } from './client'
import type { CommitmentRow, PageParams } from './types'

export function listCommitments(params: PageParams = {}): Promise<CommitmentRow[]> {
  return apiGet(`/commitments${buildQuery(params)}`)
}
