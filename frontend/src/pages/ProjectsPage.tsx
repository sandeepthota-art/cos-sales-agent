import { listProjects } from '../api/projects'
import type { ProjectRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'

export function ProjectsPage() {
  return (
    <EntityListPage<ProjectRow>
      title="Projects"
      fetcher={(limit, offset) => listProjects({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/projects/${encodeURIComponent(row.id)}`}
      searchableText={(row) => `${row.project} ${row.entity ?? ''} ${row.owner ?? ''}`}
      emptyTitle="No projects yet"
      columns={[
        { key: 'project', label: 'Project' },
        { key: 'entity', label: 'Entity' },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
        { key: 'owner', label: 'Owner' },
        { key: 'health', label: 'Health', render: (row) => <StatusBadge status={row.health} /> },
        { key: 'next_milestone', label: 'Next Milestone' },
        { key: 'due', label: 'Due' },
      ]}
    />
  )
}
