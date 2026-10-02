import { listFollowUps } from '../api/followUps'
import type { FollowUpRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

export function FollowUpsPage() {
  return (
    <EntityListPage<FollowUpRow>
      title="Follow-ups"
      fetcher={(limit, offset) => listFollowUps({ limit, offset })}
      rowKey={(row) => row.id}
      searchableText={(row) => `${row.what ?? ''} ${row.person_name ?? ''} ${row.org_name ?? ''}`}
      emptyTitle="No follow-ups yet"
      columns={[
        { key: 'what', label: 'What' },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
        { key: 'person_name', label: 'Person' },
        { key: 'org_name', label: 'Organization' },
        { key: 'audience', label: 'Audience' },
        {
          key: 'follow_up_earliest_at',
          label: 'Window',
          render: (row) =>
            row.follow_up_earliest_at
              ? `${formatDate(row.follow_up_earliest_at)} – ${formatDate(row.follow_up_latest_at)}`
              : '—',
        },
      ]}
    />
  )
}
