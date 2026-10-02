import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getPerson } from '../api/people'
import type { PersonContext } from '../api/types'
import { HIDDEN_FIELDS } from '../config/hiddenFields'
import { ErrorState } from '../components/ErrorState'
import { IdLink } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { RecordList } from '../components/RecordList'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

export function PersonDetailPage() {
  const { personId = '' } = useParams<{ personId: string }>()
  const [context, setContext] = useState<PersonContext | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getPerson(personId)
      .then((result) => {
        if (!cancelled) setContext(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load person')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [personId])

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!context) return <ErrorState message="Person not found" />

  const { person } = context

  return (
    <div>
      <PageHeader
        title={person.name || person.id}
        subtitle={person.role ?? undefined}
        breadcrumbs={[{ label: 'People', to: '/people' }, { label: person.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="card__title">Profile</p>
        <p>Email: {person.email ?? '—'}</p>
        <p>
          Organization:{' '}
          {context.organization.data ? (
            <IdLink id={(context.organization.data.id as string) ?? ''} label={(context.organization.data.name as string) ?? person.org ?? undefined} />
          ) : (
            person.org ?? '—'
          )}
        </p>
        <p>Last inbound: {formatDate(person.last_inbound)}</p>
        <p>Last outbound: {formatDate(person.last_outbound)}</p>
        <p>
          Status: <StatusBadge status={person.status} />
          {person.merged_into && (
            <>
              {' '}
              → merged into <IdLink id={person.merged_into} />
            </>
          )}
        </p>
      </div>

      <div className="section-grid">
        <RecordList title="Commitments" records={context.commitments} />
        <RecordList title="Meetings" records={context.meetings} skip={[...HIDDEN_FIELDS.meetings]} />
        <RecordList title="Follow-ups" records={context.follow_ups} skip={[...HIDDEN_FIELDS.follow_ups]} />
        <RecordList title="Projects" records={context.projects} skip={[...HIDDEN_FIELDS.projects]} />
        <RecordList title="Knowledge" records={context.knowledge} />
        <RecordList title="Reply Drafts" records={context.reply_drafts} />
        <RecordList title="Calendar Actions" records={context.calendar_actions} />
        <RecordList
          title="Other People at Organization"
          records={context.other_people_at_org.data.map((p) => ({ ...p }))}
        />
      </div>
    </div>
  )
}
