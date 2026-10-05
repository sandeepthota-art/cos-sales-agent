import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getPerson } from '../api/people'
import type { PersonContext } from '../api/types'
import { ErrorState } from '../components/ErrorState'
import { IdLink } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
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
  const orgData = context.organization.data
  const companyName = orgData?.name ?? person.org ?? undefined

  return (
    <div>
      <PageHeader
        title={person.name || person.id}
        subtitle={person.role ?? undefined}
        breadcrumbs={[{ label: 'People', to: '/people' }, { label: person.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        {companyName && (
          <p className="card__title">
            {orgData ? <IdLink id={orgData.id} label={companyName} /> : companyName}
          </p>
        )}

        {orgData?.description && (
          <p style={{ color: 'var(--color-text-muted)' }}>{orgData.description}</p>
        )}

        {person.profile_summary ? (
          <p>{person.profile_summary}</p>
        ) : (
          <p style={{ color: 'var(--color-text-muted)' }}>
            No profile yet -- this builds up as emails from this person are processed.
          </p>
        )}

        {person.recent_context && (
          <>
            <p className="card__title" style={{ marginTop: 12 }}>
              Recent context
            </p>
            <p>{person.recent_context}</p>
          </>
        )}

        {(person.key_topics?.length ?? 0) > 0 && (
          <p style={{ marginTop: 12, color: 'var(--color-text-muted)' }}>{person.key_topics!.join(' · ')}</p>
        )}

        <p style={{ marginTop: 12 }}>
          Email: {person.email ?? '—'}
          {' · '}
          Last heard from: {formatDate(person.last_inbound)}
        </p>

        {context.other_people_at_org.data.length > 0 && (
          <p>
            Also at {companyName ?? 'this organization'}:{' '}
            {context.other_people_at_org.data.map((p, index) => (
              <span key={p.id}>
                <IdLink id={p.id} label={p.name ?? p.id} />
                {index < context.other_people_at_org.data.length - 1 && ', '}
              </span>
            ))}
          </p>
        )}

        {person.merged_into && (
          <p>
            <StatusBadge status={person.status} /> → merged into <IdLink id={person.merged_into} />
          </p>
        )}
      </div>
    </div>
  )
}
