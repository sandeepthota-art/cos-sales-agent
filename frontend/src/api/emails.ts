import { apiGet, buildQuery } from './client'
import type { EmailRow, PageParams } from './types'

export function listEmails(params: PageParams = {}): Promise<EmailRow[]> {
  return apiGet(`/emails${buildQuery(params)}`)
}

export function getEmail(messageId: string): Promise<EmailRow> {
  return apiGet(`/emails/${encodeURIComponent(messageId)}`)
}
