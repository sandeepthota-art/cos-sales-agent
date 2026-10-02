import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { approveCalendarAction, ignoreCalendarAction, listCalendarActions } from '../api/calendarActions'
import type { CalendarActionRow } from '../api/types'
import { ApprovalCard } from '../components/ApprovalCard'
import { EmptyState } from '../components/EmptyState'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

interface ItemState {
  busy: boolean
  error: string | null
}

function key(action: CalendarActionRow): string {
  return `${action.thread_id}::${action.meeting_fingerprint}`
}

export function CalendarApprovalsPage() {
  const [actions, setActions] = useState<CalendarActionRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [itemStates, setItemStates] = useState<Record<string, ItemState>>({})

  function load() {
    setLoading(true)
    setError(null)
    listCalendarActions()
      .then((result) => {
        setActions(result)
        setItemStates(Object.fromEntries(result.map((a) => [key(a), { busy: false, error: null }])))
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : 'Failed to load calendar actions'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  function patchItem(itemKey: string, patch: Partial<ItemState>) {
    setItemStates((prev) => ({ ...prev, [itemKey]: { ...prev[itemKey], ...patch } }))
  }

  async function runAction(itemKey: string, action: () => Promise<CalendarActionRow>) {
    patchItem(itemKey, { busy: true, error: null })
    try {
      await action()
      load()
    } catch (err: unknown) {
      patchItem(itemKey, { busy: false, error: err instanceof ApiError ? err.message : 'Action failed' })
    }
  }

  if (loading) return <LoadingSkeleton rows={4} />

  return (
    <div>
      <PageHeader
        title="Calendar Approvals"
        subtitle="Proposed meetings awaiting approval, including those needing clarification."
      />

      {error && <ErrorState message={error} onRetry={load} />}
      {!error && actions.length === 0 && <EmptyState title="No calendar actions awaiting approval" />}

      {!error &&
        actions.map((action) => {
          const itemKey = key(action)
          const state = itemStates[itemKey] ?? { busy: false, error: null }
          return (
            <ApprovalCard
              key={itemKey}
              busy={state.busy}
              error={state.error}
              actions={[
                {
                  label: 'Approve',
                  variant: 'primary',
                  onClick: () =>
                    runAction(itemKey, () => approveCalendarAction(action.thread_id, action.meeting_fingerprint)),
                },
                {
                  label: 'Ignore',
                  variant: 'danger',
                  onClick: () =>
                    runAction(itemKey, () => ignoreCalendarAction(action.thread_id, action.meeting_fingerprint)),
                },
              ]}
            >
              <p>
                <strong>Person:</strong> {action.person_name ?? '—'}
                {action.org_name && ` (${action.org_name})`}
              </p>
              <p>
                <strong>Thread:</strong> {action.thread_id}
              </p>
              <p style={{ marginTop: 10 }}>
                <strong>{action.event.title}</strong> <StatusBadge status={action.status} />
              </p>
              <p>
                {formatDate(action.event.time?.start)} – {formatDate(action.event.time?.end)}
              </p>
              {action.reason && <p style={{ color: 'var(--color-attention)' }}>{action.reason}</p>}
            </ApprovalCard>
          )
        })}
    </div>
  )
}
