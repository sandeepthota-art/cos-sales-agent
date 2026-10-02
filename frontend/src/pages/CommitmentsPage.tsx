import { listCommitments } from '../api/commitments'
import type { CommitmentRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateOnly } from '../utils/format'

export function CommitmentsPage() {
  return (
    <EntityListPage<CommitmentRow>
      title="Commitments"
      fetcher={(limit, offset) => listCommitments({ limit, offset })}
      rowKey={(row) => row.id}
      searchableText={(row) => `${row.what} ${row.owed_by ?? ''} ${row.owed_to ?? ''}`}
      emptyTitle="No commitments yet"
      columns={[
        { key: 'what', label: 'What' },
        { key: 'class', label: 'Class', render: (row) => <StatusBadge status={row.class} /> },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
        { key: 'owed_by', label: 'Owed By' },
        { key: 'owed_to', label: 'Owed To' },
        { key: 'committed_date', label: 'Due', render: (row) => formatDateOnly(row.committed_date) },
      ]}
    />
  )
}
