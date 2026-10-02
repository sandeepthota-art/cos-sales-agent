import { KeyValueList } from './KeyValueList'

interface RecordListProps<T extends object> {
  title: string
  records: T[]
  emptyText?: string
  skip?: string[]
}

/** Renders a cross-collection "related X" array (commitments, meetings,
 * follow_ups, ...) returned by the entity-360 endpoints. These come back as
 * loosely-typed dicts (see app/entities/context.py), so each record is shown
 * via the generic KeyValueList rather than a hand-typed table. */
export function RecordList<T extends object>({
  title,
  records,
  emptyText = 'None found.',
  skip = [],
}: RecordListProps<T>) {
  return (
    <div className="card">
      <p className="card__title">{title}</p>
      {records.length === 0 ? (
        <p>{emptyText}</p>
      ) : (
        records.map((record, index) => (
          <div key={index} style={{ marginBottom: index < records.length - 1 ? 14 : 0 }}>
            <KeyValueList data={record as Record<string, unknown>} skip={skip} />
          </div>
        ))
      )}
    </div>
  )
}
