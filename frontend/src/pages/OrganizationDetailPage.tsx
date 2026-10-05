import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getOrganization } from '../api/organizations'
import type { OrganizationSummary } from '../api/types'
import { HIDDEN_FIELDS } from '../config/hiddenFields'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { RecordList } from '../components/RecordList'

export function OrganizationDetailPage() {
  const { orgId = '' } = useParams<{ orgId: string }>()
  const [summary, setSummary] = useState<OrganizationSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getOrganization(orgId)
      .then((result) => {
        if (!cancelled) setSummary(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load organization')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [orgId])

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!summary) return <ErrorState message="Organization not found" />

  const { organization, summary: related } = summary
  const hasProfile = Boolean(organization.industry || organization.description)

  return (
    <div>
      <PageHeader
        title={organization.name}
        subtitle={organization.domain ?? undefined}
        breadcrumbs={[{ label: 'Organizations', to: '/organizations' }, { label: organization.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        {hasProfile ? (
          <>
            <p className="card__title">Company Profile</p>
            {organization.industry && <p>{organization.industry}</p>}
            {organization.description && <p>{organization.description}</p>}
            {(organization.products_services?.length ?? 0) > 0 && (
              <p style={{ color: 'var(--color-text-muted)' }}>
                {organization.products_services?.join(' · ')}
              </p>
            )}
            <p style={{ marginTop: 12 }}>
              {organization.headquarters && <>{organization.headquarters}</>}
              {organization.headquarters && organization.size_estimate && ' · '}
              {organization.size_estimate && <>{organization.size_estimate}</>}
            </p>
            {organization.website && (
              <p>
                <a href={organization.website} target="_blank" rel="noreferrer">
                  {organization.website}
                </a>
              </p>
            )}
          </>
        ) : (
          <p style={{ color: 'var(--color-text-muted)' }}>No company research yet.</p>
        )}
      </div>

      <div className="section-grid">
        <RecordList title="People" records={related.people} />
        <RecordList title="Projects" records={related.projects} skip={[...HIDDEN_FIELDS.projects]} />
        <RecordList title="Commitments" records={related.related_commitments} />
        <RecordList
          title="Follow-ups"
          records={related.related_follow_ups}
          skip={[...HIDDEN_FIELDS.follow_ups]}
        />
      </div>
    </div>
  )
}
