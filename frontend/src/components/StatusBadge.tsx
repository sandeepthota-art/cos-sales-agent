interface StatusBadgeProps {
  status: string | null | undefined
}

type Tone = 'neutral' | 'positive' | 'attention' | 'critical'

const TONE_BY_STATUS: Record<string, Tone> = {
  open: 'attention',
  active: 'positive',
  approved: 'positive',
  completed: 'positive',
  done: 'positive',
  on_track: 'positive',
  won: 'positive',
  scheduled: 'positive',
  sent: 'positive',
  simulated_sent: 'positive',
  awaiting_approval: 'attention',
  needs_clarification: 'attention',
  pending: 'attention',
  at_risk: 'attention',
  edited: 'attention',
  failed: 'critical',
  lost: 'critical',
  rejected: 'critical',
  dropped: 'critical',
  off_track: 'critical',
  cancelled: 'neutral',
  merged: 'neutral',
  resolved: 'neutral',
  no_reply_required: 'neutral',
}

export function StatusBadge({ status }: StatusBadgeProps) {
  if (!status) return <span className="status-badge status-badge--neutral">—</span>
  const key = status.toLowerCase()
  const tone = TONE_BY_STATUS[key] ?? 'neutral'
  const label = status
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
  return <span className={`status-badge status-badge--${tone}`}>{label}</span>
}
