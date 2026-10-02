import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getMeetingBrief } from '../api/meetings'
import type { MeetingBrief } from '../api/types'
import { HIDDEN_FIELDS } from '../config/hiddenFields'
import { ErrorState } from '../components/ErrorState'
import { KeyValueList } from '../components/KeyValueList'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { RecordList } from '../components/RecordList'
import { StatusBadge } from '../components/StatusBadge'

function omit(record: Record<string, unknown>, fields: readonly string[]): Record<string, unknown> {
  const copy = { ...record }
  for (const field of fields) delete copy[field]
  return copy
}

interface BasisWrapped {
  basis: string
  data: unknown
}

function isBasisWrapped(value: unknown): value is BasisWrapped {
  return typeof value === 'object' && value !== null && 'data' in value
}

/** get_organization_context (app/entities/context.py) nests raw Organization/
 * Project/FollowUp documents several levels deep. Rather than generalize
 * KeyValueList's nestedSkip to be path-aware for this one deeply-nested
 * shape, this strips the known-dead fields from just this payload before it
 * reaches the generic renderer -- same hiding the Streamlit dashboard and
 * this app's other detail pages already apply, just scoped to this shape. */
function sanitizeOrganizationContext(context: Record<string, unknown>): Record<string, unknown> {
  const cleaned: Record<string, unknown> = { ...context }
  if (context.organization && typeof context.organization === 'object') {
    cleaned.organization = omit(context.organization as Record<string, unknown>, HIDDEN_FIELDS.organizations)
  }
  for (const [key, fields] of [
    ['projects', HIDDEN_FIELDS.projects],
    ['follow_ups', HIDDEN_FIELDS.follow_ups],
    ['meetings', HIDDEN_FIELDS.meetings],
  ] as const) {
    const wrapped = cleaned[key]
    if (isBasisWrapped(wrapped) && Array.isArray(wrapped.data)) {
      cleaned[key] = {
        ...wrapped,
        data: (wrapped.data as Record<string, unknown>[]).map((item) => omit(item, fields)),
      }
    }
  }
  return cleaned
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

  const projectOrPillar = (brief.meeting.project_or_pillar as string | undefined) ?? undefined

  return (
    <div>
      <PageHeader
        title="Meeting Brief"
        subtitle={projectOrPillar}
        breadcrumbs={[{ label: 'Meetings', to: '/meetings' }, { label: meetingId }]}
        actions={<StatusBadge status={brief.classification} />}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="card__title">Meeting</p>
        <KeyValueList data={brief.meeting} skip={['id', ...HIDDEN_FIELDS.meetings]} />
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
        {brief.organization_context && (
          <div className="card">
            <p className="card__title">Organization Context</p>
            <KeyValueList data={sanitizeOrganizationContext(brief.organization_context)} />
          </div>
        )}
        {brief.project_context && (
          <div className="card">
            <p className="card__title">Project Context</p>
            <KeyValueList data={brief.project_context} />
          </div>
        )}
        {brief.thread_context && (
          <div className="card">
            <p className="card__title">Thread Context</p>
            <KeyValueList data={brief.thread_context} />
          </div>
        )}
        <RecordList
          title="Previous Meetings"
          records={brief.previous_meetings}
          skip={[...HIDDEN_FIELDS.meetings]}
        />
        <RecordList title="Open Commitments" records={brief.open_commitments} />
        <RecordList
          title="Relevant Follow-ups"
          records={brief.relevant_follow_ups}
          skip={[...HIDDEN_FIELDS.follow_ups]}
        />
        <RecordList title="Relevant Knowledge" records={brief.relevant_knowledge} />
        <RecordList title="Existing Reply Drafts" records={brief.existing_reply_drafts} />
      </div>
    </div>
  )
}
