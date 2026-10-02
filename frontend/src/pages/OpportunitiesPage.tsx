import { listOpportunities } from '../api/opportunities'
import type { OpportunityRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatCurrency, formatDateOnly } from '../utils/format'

export function OpportunitiesPage() {
  return (
    <EntityListPage<OpportunityRow>
      title="Opportunities"
      fetcher={(limit, offset) => listOpportunities({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/opportunities/${encodeURIComponent(row.id)}`}
      searchableText={(row) => `${row.name} ${row.entity ?? ''} ${row.owner ?? ''}`}
      emptyTitle="No opportunities yet"
      columns={[
        { key: 'name', label: 'Name' },
        { key: 'entity', label: 'Entity' },
        { key: 'stage', label: 'Stage', render: (row) => <StatusBadge status={row.stage} /> },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
        { key: 'value', label: 'Value', render: (row) => formatCurrency(row.value, row.currency) },
        {
          key: 'expected_close_date',
          label: 'Expected Close',
          render: (row) => formatDateOnly(row.expected_close_date),
        },
        { key: 'owner', label: 'Owner' },
      ]}
    />
  )
}
