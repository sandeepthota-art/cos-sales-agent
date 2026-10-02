import { listThreads } from '../api/threads'
import type { ThreadRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { formatDate } from '../utils/format'

export function ThreadsPage() {
  return (
    <EntityListPage<ThreadRow>
      title="Threads"
      subtitle="Every conversation thread, with its participants and message count."
      fetcher={(limit, offset) => listThreads({ limit, offset })}
      rowKey={(row) => row.thread_id}
      detailRoute={(row) => `/threads/${encodeURIComponent(row.thread_id)}`}
      searchableText={(row) => `${row.normalized_subject} ${row.participant_emails.join(' ')}`}
      emptyTitle="No threads yet"
      columns={[
        { key: 'normalized_subject', label: 'Subject' },
        {
          key: 'participant_emails',
          label: 'Participants',
          render: (row) => row.participant_emails.join(', '),
        },
        { key: 'message_count', label: 'Messages' },
        { key: 'last_message_at', label: 'Last Activity', render: (row) => formatDate(row.last_message_at) },
      ]}
    />
  )
}
