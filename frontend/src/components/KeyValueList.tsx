import { formatList, formatValue, humanizeKey, isEmptyValue } from '../utils/format'

function isArrayOfObjects(value: unknown): value is Record<string, unknown>[] {
  return Array.isArray(value) && value.length > 0 && typeof value[0] === 'object' && value[0] !== null
}

interface KeyValueListProps {
  data: Record<string, unknown> | null | undefined
  skip?: string[]
  /** Skip lists for nested objects, keyed by the field name that holds them
   * -- e.g. a MeetingBrief's organization_context nests a raw Organization
   * document under "organization"; this lets that nested render hide the
   * same dead fields the top-level case would, without flattening the
   * structure or special-casing every page. */
  nestedSkip?: Record<string, string[]>
}

/** Generic fallback renderer for loosely-typed nested API payloads (e.g. a
 * MeetingBrief's classification/context blobs) -- humanizes keys, formats
 * primitives/arrays, and skips empty fields, without needing a hand-written
 * shape for every nested object the backend can return. */
export function KeyValueList({ data, skip = [], nestedSkip = {} }: KeyValueListProps) {
  if (!data) return <span>—</span>
  const entries = Object.entries(data).filter(
    ([key, value]) => !skip.includes(key) && !isEmptyValue(value),
  )
  if (entries.length === 0) return <span>—</span>

  return (
    <dl className="key-value-list">
      {entries.map(([key, value]) => (
        <div className="key-value-list__row" key={key}>
          <dt>{humanizeKey(key)}</dt>
          <dd>
            {isArrayOfObjects(value) ? (
              <ul className="key-value-list__nested-items">
                {value.map((item, index) => (
                  <li key={index}>
                    <KeyValueList data={item} skip={nestedSkip[key] ?? []} nestedSkip={nestedSkip} />
                  </li>
                ))}
              </ul>
            ) : typeof value === 'object' && value !== null && !Array.isArray(value) ? (
              <KeyValueList
                data={value as Record<string, unknown>}
                skip={nestedSkip[key] ?? []}
                nestedSkip={nestedSkip}
              />
            ) : Array.isArray(value) ? (
              formatList(value)
            ) : (
              formatValue(value)
            )}
          </dd>
        </div>
      ))}
    </dl>
  )
}
