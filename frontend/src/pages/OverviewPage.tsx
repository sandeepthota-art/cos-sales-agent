import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getOverview } from '../api/overview'
import type { Overview } from '../api/types'
import { DataTable } from '../components/DataTable'
import { ErrorState } from '../components/ErrorState'
import { IdLink } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate, formatDateOnly } from '../utils/format'

const METRIC_LABELS: Record<keyof Overview['metrics'], string> = {
  emails_processed: 'Emails Processed',
  threads: 'Threads',
  knowledge_items: 'Knowledge Items',
  pending_replies: 'Pending Replies',
  pending_calendar_actions: 'Pending Calendar Actions',
  failures: 'Processing Failures',
}

export function OverviewPage() {
  const navigate = useNavigate()
  const [overview, setOverview] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getOverview()
      .then((result) => {
        if (!cancelled) setOverview(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load overview')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [reloadToken])

  return (
    <div>
      <PageHeader title="Overview" subtitle="What needs your attention right now." />

      {loading && <LoadingSkeleton rows={4} />}
      {!loading && error && <ErrorState message={error} onRetry={() => setReloadToken((t) => t + 1)} />}

      {!loading && !error && overview && (
        <>
          <div className="metric-grid">
            {(Object.keys(METRIC_LABELS) as (keyof Overview['metrics'])[]).map((key) => (
              <div className="metric-card" key={key}>
                <div className="metric-card__value">{overview.metrics[key]}</div>
                <div className="metric-card__label">{METRIC_LABELS[key]}</div>
              </div>
            ))}
          </div>

          <div className="section-grid">
            <div className="card">
              <p className="card__title">Pending Replies</p>
              <DataTable
                rows={overview.attention.pending_replies}
                rowKey={(row) => row.reply_id}
                emptyTitle="No replies awaiting approval"
                columns={[
                  { key: 'recipient', label: 'To' },
                  {
                    key: 'draft',
                    label: 'Subject',
                    render: (row) => row.draft.subject,
                  },
                ]}
              />
            </div>

            <div className="card">
              <p className="card__title">Overdue Follow-ups</p>
              <DataTable
                rows={overview.attention.overdue_follow_ups}
                rowKey={(row) => row.id}
                emptyTitle="Nothing overdue"
                columns={[
                  { key: 'what', label: 'What' },
                  { key: 'person_name', label: 'Person' },
                  {
                    key: 'status',
                    label: 'Status',
                    render: (row) => <StatusBadge status={row.status} />,
                  },
                ]}
              />
            </div>

            <div className="card">
              <p className="card__title">Commitments Due</p>
              <DataTable
                rows={overview.attention.commitments_due}
                rowKey={(row) => row.id}
                emptyTitle="No commitments due soon"
                columns={[
                  { key: 'what', label: 'What' },
                  { key: 'owed_by', label: 'Owed By' },
                  {
                    key: 'committed_date',
                    label: 'Due',
                    render: (row) => formatDateOnly(row.committed_date),
                  },
                ]}
              />
            </div>

            <div className="card">
              <p className="card__title">Upcoming Meetings</p>
              <DataTable
                rows={overview.attention.upcoming_meetings}
                rowKey={(row) => row.id}
                emptyTitle="No upcoming meetings"
                columns={[
                  { key: 'title', label: 'Meeting' },
                  { key: 'date', label: 'Date', render: (row) => formatDate(row.date) },
                ]}
                onRowClick={(row) => navigate(`/meetings/${row.id}/brief`)}
              />
            </div>

            <div className="card">
              <p className="card__title">Active Projects</p>
              <DataTable
                rows={overview.attention.active_projects}
                rowKey={(row) => row.id}
                emptyTitle="No active projects"
                columns={[
                  { key: 'project', label: 'Project', render: (row) => <IdLink id={row.id} label={row.project} /> },
                  { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
                  { key: 'owner', label: 'Owner' },
                ]}
              />
            </div>
          </div>
        </>
      )}
    </div>
  )
}
