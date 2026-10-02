import { listMeetings } from '../api/meetings'
import type { MeetingRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { formatDate } from '../utils/format'

export function MeetingsPage() {
  return (
    <EntityListPage<MeetingRow>
      title="Meetings"
      subtitle="Select a meeting to open its preparation brief."
      fetcher={(limit, offset) => listMeetings({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/meetings/${encodeURIComponent(row.id)}/brief`}
      searchableText={(row) => `${row.title ?? ''} ${(row.attendees ?? []).join(' ')}`}
      emptyTitle="No meetings yet"
      columns={[
        { key: 'title', label: 'Meeting' },
        { key: 'date', label: 'Date', render: (row) => formatDate(row.date) },
        { key: 'attendees', label: 'Attendees', render: (row) => (row.attendees ?? []).join(', ') || '—' },
        { key: 'project_or_pillar', label: 'Project / Pillar' },
        { key: 'actionable', label: 'Actionable', render: (row) => (row.actionable ? 'Yes' : 'No') },
      ]}
    />
  )
}
