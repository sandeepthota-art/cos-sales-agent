import { listPersonalItems } from '../api/personalItems'
import type { PersonalItemRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateOnly } from '../utils/format'

export function PersonalItemsPage() {
  return (
    <EntityListPage<PersonalItemRow>
      title="Personal Items"
      fetcher={(limit, offset) => listPersonalItems({ limit, offset })}
      rowKey={(row) => row.id}
      searchableText={(row) => `${row.description ?? ''} ${row.type ?? ''}`}
      emptyTitle="No personal items yet"
      columns={[
        { key: 'description', label: 'Description' },
        { key: 'type', label: 'Type' },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
        { key: 'date_or_deadline', label: 'Deadline', render: (row) => formatDateOnly(row.date_or_deadline) },
      ]}
    />
  )
}
