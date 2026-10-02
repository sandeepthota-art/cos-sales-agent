import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { useListQuery } from '../hooks/useListQuery'
import { DataTable, type ColumnDef } from './DataTable'
import { ErrorState } from './ErrorState'
import { PageHeader } from './PageHeader'
import { Pagination } from './Pagination'
import { SearchBox } from './SearchBox'

interface EntityListPageProps<T> {
  title: string
  subtitle?: string
  fetcher: (limit: number, offset: number) => Promise<T[]>
  columns: ColumnDef<T>[]
  rowKey: (row: T) => string
  detailRoute?: (row: T) => string
  searchableText?: (row: T) => string
  emptyTitle?: string
}

export function EntityListPage<T>({
  title,
  subtitle,
  fetcher,
  columns,
  rowKey,
  detailRoute,
  searchableText,
  emptyTitle,
}: EntityListPageProps<T>) {
  const navigate = useNavigate()
  const [filterInput, setFilterInput] = useState('')
  const filter = useDebouncedValue(filterInput, 200)
  const { rows, loading, error, page, hasPrev, hasNext, next, prev, reload } = useListQuery(fetcher)

  const visibleRows = useMemo(() => {
    if (!searchableText || !filter.trim()) return rows
    const needle = filter.trim().toLowerCase()
    return rows.filter((row) => searchableText(row).toLowerCase().includes(needle))
  }, [rows, filter, searchableText])

  return (
    <div>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={
          searchableText ? (
            <SearchBox value={filterInput} onChange={setFilterInput} />
          ) : undefined
        }
      />

      {error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : (
        <>
          <DataTable
            columns={columns}
            rows={visibleRows}
            rowKey={rowKey}
            loading={loading}
            emptyTitle={emptyTitle}
            onRowClick={detailRoute ? (row) => navigate(detailRoute(row)) : undefined}
          />
          {!loading && <Pagination page={page} hasPrev={hasPrev} hasNext={hasNext} onPrev={prev} onNext={next} />}
        </>
      )}
    </div>
  )
}
