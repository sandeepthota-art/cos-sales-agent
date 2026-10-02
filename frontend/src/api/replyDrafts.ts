import { apiGet, apiPost, buildQuery } from './client'
import type { ReplyDraftRow } from './types'

export function listReplyDrafts(statusFilter = 'awaiting_approval'): Promise<ReplyDraftRow[]> {
  return apiGet(`/reply-drafts${buildQuery({ status_filter: statusFilter })}`)
}

export function approveReplyDraft(replyId: string): Promise<ReplyDraftRow> {
  return apiPost(`/reply-drafts/${encodeURIComponent(replyId)}/approve`)
}

export function rejectReplyDraft(replyId: string): Promise<ReplyDraftRow> {
  return apiPost(`/reply-drafts/${encodeURIComponent(replyId)}/reject`)
}

export function editReplyDraft(
  replyId: string,
  edit: { subject: string; body: string },
): Promise<ReplyDraftRow> {
  return apiPost(`/reply-drafts/${encodeURIComponent(replyId)}/edit`, edit)
}
