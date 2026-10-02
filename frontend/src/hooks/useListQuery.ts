import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'

export interface ListQueryResult<T> {
  rows: T[]
  loading: boolean
  error: string | null
  page: number
  hasPrev: boolean
  hasNext: boolean
  next: () => void
  prev: () => void
  reload: () => void
}

const DEFAULT_LIMIT = 25

/** Backend list endpoints take limit/offset but return no total count, so
 * paging is "load more while a full page comes back" rather than numbered
 * pages -- an honest reflection of what the API can actually tell us. */
export function useListQuery<T>(
  fetcher: (limit: number, offset: number) => Promise<T[]>,
  limit: number = DEFAULT_LIMIT,
  deps: unknown[] = [],
): ListQueryResult<T> {
  const [page, setPage] = useState(0)
  const [rows, setRows] = useState<T[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const load = useCallback(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetcher(limit, page * limit)
      .then((result) => {
        if (!cancelled) setRows(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load data')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, limit, reloadToken, ...deps])

  useEffect(() => load(), [load])

  // Reset to page 0 whenever the query identity (deps) changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setPage(0), deps)

  return {
    rows,
    loading,
    error,
    page,
    hasPrev: page > 0,
    hasNext: rows.length === limit,
    next: () => setPage((p) => p + 1),
    prev: () => setPage((p) => Math.max(0, p - 1)),
    reload: () => setReloadToken((t) => t + 1),
  }
}
