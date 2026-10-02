import { useEffect, useState, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getProject, updateProject } from '../api/projects'
import type { ProjectFieldsUpdate, ProjectSummary } from '../api/types'
import { HIDDEN_FIELDS } from '../config/hiddenFields'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { RecordList } from '../components/RecordList'
import { StatusBadge } from '../components/StatusBadge'

function fieldsFromProject(project: ProjectSummary['project']): ProjectFieldsUpdate {
  return {
    status: project.status ?? '',
    owner: project.owner ?? '',
    health: project.health ?? '',
    next_milestone: project.next_milestone ?? '',
    due: project.due ?? '',
  }
}

export function ProjectDetailPage() {
  const { projectId = '' } = useParams<{ projectId: string }>()
  const [summary, setSummary] = useState<ProjectSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<ProjectFieldsUpdate>({})
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getProject(projectId)
      .then((result) => {
        if (cancelled) return
        setSummary(result)
        setForm(fieldsFromProject(result.project))
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load project')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [projectId])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSaving(true)
    setSaveError(null)
    try {
      const updated = await updateProject(projectId, form)
      setSummary((prev) => (prev ? { ...prev, project: updated } : prev))
      setSavedAt(Date.now())
    } catch (err: unknown) {
      setSaveError(err instanceof ApiError ? err.message : 'Failed to save changes')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!summary) return <ErrorState message="Project not found" />

  const { project } = summary

  return (
    <div>
      <PageHeader
        title={project.project}
        subtitle={project.entity ?? undefined}
        breadcrumbs={[{ label: 'Projects', to: '/projects' }, { label: project.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="card__title">Status</p>
        <p style={{ marginBottom: 14 }}>
          <StatusBadge status={project.status} /> · Health: <StatusBadge status={project.health} />
        </p>
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="status">Status</label>
            <input
              id="status"
              value={form.status ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, status: event.target.value }))}
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
            <label htmlFor="health">Health</label>
            <input
              id="health"
              value={form.health ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, health: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="next_milestone">Next Milestone</label>
            <input
              id="next_milestone"
              value={form.next_milestone ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, next_milestone: event.target.value }))}
            />
          </div>
          <div className="field">
            <label htmlFor="due">Due</label>
            <input
              id="due"
              value={form.due ?? ''}
              onChange={(event) => setForm((f) => ({ ...f, due: event.target.value }))}
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
                setForm(fieldsFromProject(project))
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
        <RecordList title="Related Commitments" records={summary.related_commitments} />
        <RecordList
          title="Related Follow-ups"
          records={summary.related_follow_ups}
          skip={[...HIDDEN_FIELDS.follow_ups]}
        />
      </div>
    </div>
  )
}
