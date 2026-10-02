import { listEmails } from '../api/emails'
import type { EmailRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

function participant(p: EmailRow['from']): string {
  return p.name || p.email || '—'
}

export function EmailsPage() {
  return (
    <EntityListPage<EmailRow>
      title="Emails"
      subtitle="Newest first. Select an email to view its full conversation."
      fetcher={(limit, offset) => listEmails({ limit, offset })}
      rowKey={(row) => row.message_id}
      detailRoute={(row) => `/threads/${encodeURIComponent(row.thread_id)}`}
      searchableText={(row) => `${row.subject} ${participant(row.from)}`}
      emptyTitle="No emails yet"
      columns={[
        { key: 'subject', label: 'Subject' },
        { key: 'from', label: 'From', render: (row) => participant(row.from) },
        { key: 'to', label: 'To', render: (row) => row.to?.map(participant).join(', ') || '—' },
        { key: 'timestamp', label: 'Received', render: (row) => formatDate(row.timestamp) },
        { key: 'label_applied', label: 'Label', render: (row) => <StatusBadge status={row.label_applied} /> },
      ]}
    />
  )
}
