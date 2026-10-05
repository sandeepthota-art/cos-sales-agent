import { listPeople } from '../api/people'
import type { PersonRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

export function PeoplePage() {
  return (
    <EntityListPage<PersonRow>
      title="People"
      fetcher={(limit, offset) => listPeople({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/people/${encodeURIComponent(row.id)}`}
      searchableText={(row) => `${row.name ?? ''} ${row.email ?? ''} ${row.company ?? ''}`}
      emptyTitle="No people yet"
      columns={[
        { key: 'name', label: 'Name' },
        { key: 'email', label: 'Email' },
        { key: 'company', label: 'Organization' },
        { key: 'role', label: 'Role' },
        {
          key: 'profile_summary',
          label: 'Profile',
          render: (row) =>
            row.profile_summary ?? (row.key_topics?.length ? row.key_topics.join(', ') : '—'),
        },
        { key: 'last_inbound', label: 'Last Inbound', render: (row) => formatDate(row.last_inbound) },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
      ]}
    />
  )
}
