import type { ReactNode } from 'react'

export interface ApprovalAction {
  label: string
  onClick: () => void
  variant?: 'primary' | 'secondary' | 'danger'
}

interface ApprovalCardProps {
  children: ReactNode
  actions: ApprovalAction[]
  busy?: boolean
  error?: string | null
}

export function ApprovalCard({ children, actions, busy, error }: ApprovalCardProps) {
  return (
    <div className="approval-card">
      <div className="approval-card__body">{children}</div>
      {error && (
        <p className="approval-card__error" role="alert">
          {error}
        </p>
      )}
      <div className="approval-card__actions">
        {actions.map((action) => (
          <button
            key={action.label}
            type="button"
            className={`button button--${action.variant ?? 'secondary'}`}
            onClick={action.onClick}
            disabled={busy}
          >
            {busy ? 'Working…' : action.label}
          </button>
        ))}
      </div>
    </div>
  )
}
