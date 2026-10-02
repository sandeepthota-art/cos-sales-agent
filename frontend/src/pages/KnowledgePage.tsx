import { listKnowledge } from '../api/knowledge'
import type { KnowledgeItemRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'

export function KnowledgePage() {
  return (
    <EntityListPage<KnowledgeItemRow>
      title="Knowledge"
      subtitle="Facts extracted from conversations."
      fetcher={(limit, offset) => listKnowledge({ limit, offset })}
      rowKey={(row) => `${row.subject_key}-${row.predicate}`}
      searchableText={(row) => `${row.subject_key} ${row.predicate} ${row.current_value}`}
      emptyTitle="No knowledge extracted yet"
      columns={[
        { key: 'subject_key', label: 'Subject' },
        { key: 'predicate', label: 'Predicate' },
        { key: 'current_value', label: 'Value' },
        { key: 'basis', label: 'Basis' },
        { key: 'confidence', label: 'Confidence', render: (row) => (row.confidence ?? 0).toFixed(2) },
      ]}
    />
  )
}
