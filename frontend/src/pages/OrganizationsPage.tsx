import { listOrganizations } from '../api/organizations'
import type { OrganizationRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'

export function OrganizationsPage() {
  return (
    <EntityListPage<OrganizationRow>
      title="Organizations"
      fetcher={(limit, offset) => listOrganizations({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/organizations/${encodeURIComponent(row.id)}`}
      searchableText={(row) => `${row.name} ${row.domain ?? ''}`}
      emptyTitle="No organizations yet"
      columns={[
        { key: 'name', label: 'Name' },
        { key: 'domain', label: 'Domain' },
      ]}
    />
  )
}
