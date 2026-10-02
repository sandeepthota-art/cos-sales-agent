import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { approveReplyDraft, editReplyDraft, listReplyDrafts, rejectReplyDraft } from '../api/replyDrafts'
import type { ReplyDraftRow } from '../api/types'
import { ApprovalCard } from '../components/ApprovalCard'
import { EmptyState } from '../components/EmptyState'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { formatDate } from '../utils/format'

interface ItemState {
  busy: boolean
  error: string | null
  editing: boolean
  subjectDraft: string
  bodyDraft: string
}

function initialItemState(draft: ReplyDraftRow): ItemState {
  return {
    busy: false,
    error: null,
    editing: false,
    subjectDraft: draft.draft.subject,
    bodyDraft: draft.draft.body,
  }
}

export function ReplyApprovalsPage() {
  const [drafts, setDrafts] = useState<ReplyDraftRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [itemStates, setItemStates] = useState<Record<string, ItemState>>({})

  function load() {
    setLoading(true)
    setError(null)
    listReplyDrafts('awaiting_approval')
      .then((result) => {
        setDrafts(result)
        setItemStates(Object.fromEntries(result.map((d) => [d.reply_id, initialItemState(d)])))
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : 'Failed to load reply drafts'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  function patchItem(replyId: string, patch: Partial<ItemState>) {
    setItemStates((prev) => ({ ...prev, [replyId]: { ...prev[replyId], ...patch } }))
  }

  async function runAction(replyId: string, action: () => Promise<ReplyDraftRow>) {
    patchItem(replyId, { busy: true, error: null })
    try {
      await action()
      load()
    } catch (err: unknown) {
      patchItem(replyId, { busy: false, error: err instanceof ApiError ? err.message : 'Action failed' })
    }
  }

  if (loading) return <LoadingSkeleton rows={4} />

  return (
    <div>
      <PageHeader title="Reply Approvals" subtitle="Drafts awaiting a human decision before sending." />

      {error && <ErrorState message={error} onRetry={load} />}
      {!error && drafts.length === 0 && <EmptyState title="No reply drafts awaiting approval" />}

      {!error &&
        drafts.map((draft) => {
          const state = itemStates[draft.reply_id] ?? initialItemState(draft)
          return (
            <ApprovalCard
              key={draft.reply_id}
              busy={state.busy}
              error={state.error}
              actions={
                state.editing
                  ? [
                      {
                        label: 'Save Edit',
                        variant: 'primary',
                        onClick: () =>
                          runAction(draft.reply_id, () =>
                            editReplyDraft(draft.reply_id, {
                              subject: state.subjectDraft,
                              body: state.bodyDraft,
                            }),
                          ),
                      },
                      {
                        label: 'Cancel',
                        onClick: () => patchItem(draft.reply_id, { editing: false }),
                      },
                    ]
                  : [
                      {
                        label: 'Approve',
                        variant: 'primary',
                        onClick: () => runAction(draft.reply_id, () => approveReplyDraft(draft.reply_id)),
                      },
                      {
                        label: 'Edit',
                        onClick: () => patchItem(draft.reply_id, { editing: true }),
                      },
                      {
                        label: 'Reject',
                        variant: 'danger',
                        onClick: () => runAction(draft.reply_id, () => rejectReplyDraft(draft.reply_id)),
                      },
                    ]
              }
            >
              <p>
                <strong>To:</strong> {draft.recipient ?? '—'}
                {draft.org_name && ` (${draft.org_name})`}
              </p>
              <p>
                <strong>Thread:</strong> {draft.thread_id}
                {draft.created_at && ` · Drafted ${formatDate(draft.created_at)}`}
              </p>
              {state.editing ? (
                <>
                  <div className="field">
                    <label htmlFor={`subject-${draft.reply_id}`}>Subject</label>
                    <input
                      id={`subject-${draft.reply_id}`}
                      value={state.subjectDraft}
                      onChange={(event) => patchItem(draft.reply_id, { subjectDraft: event.target.value })}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor={`body-${draft.reply_id}`}>Body</label>
                    <textarea
                      id={`body-${draft.reply_id}`}
                      rows={6}
                      value={state.bodyDraft}
                      onChange={(event) => patchItem(draft.reply_id, { bodyDraft: event.target.value })}
                    />
                  </div>
                </>
              ) : (
                <>
                  <p style={{ marginTop: 10 }}>
                    <strong>{draft.draft.subject}</strong>
                  </p>
                  <p style={{ whiteSpace: 'pre-wrap' }}>{draft.draft.body}</p>
                </>
              )}
            </ApprovalCard>
          )
        })}
    </div>
  )
}
