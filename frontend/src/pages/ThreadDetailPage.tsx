import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { listReplyDrafts } from '../api/replyDrafts'
import { getThread, getThreadContextVersions } from '../api/threads'
import type { ContextVersion, ReplyDraftRow, ThreadDetail } from '../api/types'
import { ErrorState } from '../components/ErrorState'
import { KeyValueList } from '../components/KeyValueList'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { formatDate } from '../utils/format'

type Tab = 'conversation' | 'context'

export function ThreadDetailPage() {
  const { threadId = '' } = useParams<{ threadId: string }>()
  const [thread, setThread] = useState<ThreadDetail | null>(null)
  const [contextVersions, setContextVersions] = useState<ContextVersion[]>([])
  const [relatedDraft, setRelatedDraft] = useState<ReplyDraftRow | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('conversation')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([
      getThread(threadId),
      getThreadContextVersions(threadId),
      listReplyDrafts('awaiting_approval'),
    ])
      .then(([threadResult, versions, drafts]) => {
        if (cancelled) return
        setThread(threadResult)
        setContextVersions(versions)
        setRelatedDraft(drafts.find((draft) => draft.thread_id === threadId) ?? null)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load thread')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [threadId])

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!thread) return <ErrorState message="Thread not found" />

  return (
    <div>
      <PageHeader
        title={thread.normalized_subject || thread.thread_id}
        subtitle={`${thread.participant_emails.join(', ')} · ${thread.message_count} messages`}
        breadcrumbs={[{ label: 'Threads', to: '/threads' }, { label: thread.thread_id }]}
      />

      <div className="approval-card__actions" style={{ marginBottom: 16 }}>
        <button
          type="button"
          className={`button button--${tab === 'conversation' ? 'primary' : 'secondary'}`}
          onClick={() => setTab('conversation')}
        >
          Conversation
        </button>
        <button
          type="button"
          className={`button button--${tab === 'context' ? 'primary' : 'secondary'}`}
          onClick={() => setTab('context')}
        >
          Context Evolution
        </button>
      </div>

      {tab === 'conversation' && (
        <div>
          {relatedDraft && (
            <div className="card" style={{ marginBottom: 16 }}>
              <p className="card__title">Pending Reply Draft</p>
              <p>
                <strong>{relatedDraft.draft.subject}</strong>
              </p>
              <p>{relatedDraft.draft.body}</p>
            </div>
          )}
          {thread.messages.map((message) => (
            <div className="card" key={message.message_id}>
              <p className="card__title">
                {message.from.name || message.from.email} · {formatDate(message.timestamp)}
              </p>
              <p>
                <strong>{message.subject}</strong>
              </p>
              <p style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{message.body}</p>
            </div>
          ))}
        </div>
      )}

      {tab === 'context' && (
        <div>
          {contextVersions.length === 0 && <p>No context snapshots recorded for this thread yet.</p>}
          {contextVersions.map((version) => (
            <div className="card" key={version.context_version}>
              <p className="card__title">Version {version.context_version}</p>
              <KeyValueList data={version.context} />
              {version.changes_from_previous_context && (
                <>
                  <p className="card__title" style={{ marginTop: 12 }}>
                    Changes From Previous
                  </p>
                  <KeyValueList data={version.changes_from_previous_context} />
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
