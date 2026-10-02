import { useEffect, useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getOpportunity, updateOpportunity } from '../api/opportunities'
import type { OpportunityFieldsUpdate, OpportunityRow } from '../api/types'
import { ErrorState } from '../components/ErrorState'
import { IdLinkList } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { formatCurrency } from '../utils/format'

function fieldsFromOpportunity(opportunity: OpportunityRow): OpportunityFieldsUpdate {
  return {
    stage: opportunity.stage ?? '',
    owner: opportunity.owner ?? '',
    value: opportunity.value ?? undefined,
    currency: opportunity.currency ?? '',
    expected_close_date: opportunity.expected_close_date ?? '',
    next_action: opportunity.next_action ?? '',
  }
}

export function OpportunityDetailPage() {
  const { opportunityId = '' } = useParams<{ opportunityId: string }>()
  const [opportunity, setOpportunity] = useState<OpportunityRow | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<OpportunityFieldsUpdate>({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getOpportunity(opportunityId)
      .then((result) => {
        if (cancelled) return
        setOpportunity(result)
        setForm(fieldsFromOpportunity(result))
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load opportunity')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [opportunityId])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSaving(true)
    setSaveError(null)
    try {
      const updated = await updateOpportunity(opportunityId, form)
      setOpportunity(updated)
      setSavedAt(Date.now())
    } catch (err: unknown) {
      setSaveError(err instanceof ApiError ? err.message : 'Failed to save changes')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!opportunity) return <ErrorState message="Opportunity not found" />

  return (
    <div>
      <PageHeader
        title={opportunity.name}
        subtitle={opportunity.entity ?? undefined}
        breadcrumbs={[{ label: 'Opportunities', to: '/opportunities' }, { label: opportunity.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="card__title">Deal</p>
        <p style={{ marginBottom: 14 }}>
          <StatusBadge status={opportunity.stage} /> · <StatusBadge status={opportunity.status} /> ·{' '}
          {formatCurrency(opportunity.value, opportunity.currency)}
        </p>
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="stage">Stage</label>
            <input
              id="stage"
              value={form.stage ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, stage: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="owner">Owner</label>
            <input
              id="owner"
              value={form.owner ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, owner: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="value">Value</label>
            <input
              id="value"
              type="number"
              value={form.value ?? ''}
              onChange={(event) =>
                setForm((f) => ({ ...f, value: event.target.value ? Number(event.target.value) : undefined }))
              }
            />
          </div>
          <div className="field">
            <label htmlFor="currency">Currency</label>
            <input
              id="currency"
              value={form.currency ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, currency: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="expected_close_date">Expected Close Date</label>
            <input
              id="expected_close_date"
              value={form.expected_close_date ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, expected_close_date: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="next_action">Next Action</label>
            <input
              id="next_action"
              value={form.next_action ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, next_action: event.target.value }))}
            />
          </div>
          {saveError && (
            <p className="approval-card__error" role="alert">
              {saveError}
            </p>
          )}
          {savedAt && !saveError && <p style={{ color: 'var(--color-positive)', marginBottom: 10 }}>Saved.</p>}
          <div className="approval-card__actions" style={{ marginTop: 0 }}>
            <button type="submit" className="button button--primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save changes'}
            </button>
            <button
              type="button"
              className="button button--secondary"
              disabled={saving}
              onClick={() => {
                setForm(fieldsFromOpportunity(opportunity))
                setSaveError(null)
                setSavedAt(null)
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      </div>

      <div className="section-grid">
        <div className="card">
          <p className="card__title">Related Projects</p>
          <IdLinkList ids={opportunity.project_ids} />
        </div>
        <div className="card">
          <p className="card__title">Related People</p>
          <IdLinkList ids={opportunity.person_ids} />
        </div>
        <div className="card">
          <p className="card__title">Related Meetings</p>
          <IdLinkList ids={opportunity.meeting_ids} />
        </div>
        <div className="card">
          <p className="card__title">Buying Signals</p>
          <p>{(opportunity.buying_signals ?? []).join(', ') || '—'}</p>
        </div>
      </div>
    </div>
  )
}
