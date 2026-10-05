import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getMeetingBrief } from '../api/meetings'
import type {
  CommitmentRow,
  MeetingBrief,
  MeetingBriefFollowUp,
  MeetingBriefKnowledgeItem,
  MeetingBriefReplyDraft,
  PersonContext,
} from '../api/types'
import { ErrorState } from '../components/ErrorState'
import { IdLink } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

function AttendeeCard({ context }: { context: PersonContext }) {
  const { person } = context
  const orgData = context.organization.data
  const companyName = orgData?.name ?? person.org ?? undefined

  return (
    <div style={{ marginBottom: 12 }}>
      <p className="card__title">
        <IdLink id={person.id} label={person.name || person.id} />
        {companyName && (
          <>
            {' · '}
            {orgData ? <IdLink id={orgData.id} label={companyName} /> : companyName}
          </>
        )}
      </p>
      {person.profile_summary && <p>{person.profile_summary}</p>}
      {person.recent_context && <p style={{ color: 'var(--color-text-muted)' }}>{person.recent_context}</p>}
    </div>
  )
}

function CommitmentLine({ commitment }: { commitment: CommitmentRow }) {
  return (
    <p>
      {commitment.what}
      {' — '}
      {commitment.owed_by ?? '—'} {'→'} {commitment.owed_to ?? '—'}
      {commitment.committed_date && <> · due {formatDate(commitment.committed_date)}</>}
      {' '}
      <StatusBadge status={commitment.status} />
    </p>
  )
}

function FollowUpLine({ followUp }: { followUp: MeetingBriefFollowUp }) {
  const window =
    followUp.follow_up_earliest_at && followUp.follow_up_latest_at
      ? `${formatDate(followUp.follow_up_earliest_at)} – ${formatDate(followUp.follow_up_latest_at)}`
      : null

  return (
    <p>
      {followUp.audience ?? 'Follow-up'}
      {window && <> · due {window}</>}
      {' '}
      <StatusBadge status={followUp.status} />
    </p>
  )
}

export function MeetingBriefPage() {
  const { meetingId = '' } = useParams<{ meetingId: string }>()
  const [brief, setBrief] = useState<MeetingBrief | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getMeetingBrief(meetingId)
      .then((result) => {
        if (!cancelled) setBrief(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load meeting brief')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [meetingId])

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!brief) return <ErrorState message="Meeting not found" />

  const { meeting } = brief
  const orgData = brief.organization_context?.organization
  const thread = brief.thread_context?.thread

  return (
    <div>
      <PageHeader
        title="Meeting Brief"
        subtitle={meeting.project_or_pillar ?? undefined}
        breadcrumbs={[{ label: 'Meetings', to: '/meetings' }, { label: meetingId }]}
        actions={<StatusBadge status={brief.classification} />}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="card__title">Meeting</p>
        <p>{formatDate(meeting.date)}</p>
        {(meeting.attendees?.length ?? 0) > 0 && <p>Attendees: {meeting.attendees!.join(', ')}</p>}
        <p>{meeting.actionable ? 'Actionable' : 'Not actionable'}</p>
        {(meeting.actions_raised?.length ?? 0) > 0 && (
          <ul>
            {meeting.actions_raised!.map((action, index) => (
              <li key={index}>{action}</li>
            ))}
          </ul>
        )}
      </div>

      {brief.attendee_resolution_notes.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <p className="card__title">Attendee Resolution Notes</p>
          {brief.attendee_resolution_notes.map((note, index) => (
            <p key={index}>{note}</p>
          ))}
        </div>
      )}

      <div className="section-grid">
        {brief.attendee_contexts.length > 0 && (
          <div className="card">
            <p className="card__title">Attendees</p>
            {brief.attendee_contexts.map((context) => (
              <AttendeeCard key={context.person.id} context={context} />
            ))}
          </div>
        )}

        {orgData && (
          <div className="card">
            <p className="card__title">
              <IdLink id={orgData.id} label={orgData.name} />
            </p>
            {orgData.industry && <p style={{ color: 'var(--color-text-muted)' }}>{orgData.industry}</p>}
            {orgData.description && <p>{orgData.description}</p>}
          </div>
        )}

        {thread && (
          <div className="card">
            <p className="card__title">Thread</p>
            <p>
              {thread.normalized_subject} · last active {formatDate(thread.last_message_at)}
            </p>
          </div>
        )}

        <div className="card">
          <p className="card__title">Previous Meetings</p>
          {brief.previous_meetings.length === 0 ? (
            <p style={{ color: 'var(--color-text-muted)' }}>None</p>
          ) : (
            brief.previous_meetings.map((previous) => (
              <p key={previous.id}>
                {formatDate(previous.date)}
                {(previous.actions_raised?.length ?? 0) > 0 && <> — {previous.actions_raised!.join(', ')}</>}
              </p>
            ))
          )}
        </div>

        <div className="card">
          <p className="card__title">Open Commitments</p>
          {brief.open_commitments.length === 0 ? (
            <p style={{ color: 'var(--color-text-muted)' }}>None</p>
          ) : (
            brief.open_commitments.map((commitment) => <CommitmentLine key={commitment.id} commitment={commitment} />)
          )}
        </div>

        <div className="card">
          <p className="card__title">Relevant Follow-ups</p>
          {brief.relevant_follow_ups.length === 0 ? (
            <p style={{ color: 'var(--color-text-muted)' }}>None</p>
          ) : (
            brief.relevant_follow_ups.map((followUp) => <FollowUpLine key={followUp.id} followUp={followUp} />)
          )}
        </div>

        <div className="card">
          <p className="card__title">Relevant Knowledge</p>
          {brief.relevant_knowledge.length === 0 ? (
            <p style={{ color: 'var(--color-text-muted)' }}>None</p>
          ) : (
            <ul>
              {brief.relevant_knowledge.map((item: MeetingBriefKnowledgeItem) => (
                <li key={item.knowledge_id}>{item.current_value}</li>
              ))}
            </ul>
          )}
        </div>

        <div className="card">
          <p className="card__title">Existing Reply Drafts</p>
          {brief.existing_reply_drafts.length === 0 ? (
            <p style={{ color: 'var(--color-text-muted)' }}>None</p>
          ) : (
            brief.existing_reply_drafts.map((reply: MeetingBriefReplyDraft) => (
              <p key={reply.reply_id}>
                {reply.draft.subject} <StatusBadge status={reply.status} />
              </p>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
