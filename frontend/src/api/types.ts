// Mirrors app/api/routers/*.py response shapes exactly -- see
// docs/REACT_MIGRATION_PLAN.md and app/ui/column_descriptions.py for the
// field-by-field provenance of every property below. Frozen collections
// (Emails/Threads/People) are typed from their real schema, not guessed.

export interface EmailParticipant {
  name?: string | null
  email?: string | null
}

export interface ProcessingStatus {
  stage: string
  error?: string | null
}

export interface EmailRow {
  message_id: string
  thread_id: string
  source_message_id?: string | null
  source_thread_id?: string | null
  from: EmailParticipant
  to: EmailParticipant[]
  cc?: EmailParticipant[]
  subject: string
  body: string
  timestamp: string
  goal_pillar?: string | null
  label_applied?: string | null
  entities_referenced?: Record<string, string[]>
  attachments?: string[]
  in_reply_to?: string | null
  references?: string[]
  labels?: string[]
  processing_status?: ProcessingStatus
}

export interface ThreadRow {
  thread_id: string
  normalized_subject: string
  participant_emails: string[]
  message_count: number
  last_message_at: string
}

export interface ThreadMessage {
  message_id: string
  from: EmailParticipant
  to: EmailParticipant[]
  cc: EmailParticipant[]
  subject: string
  timestamp: string
  labels: string[]
  body: string
  processing_status?: ProcessingStatus
}

export interface ThreadDetail extends ThreadRow {
  messages: ThreadMessage[]
  latest_context: Record<string, unknown> | null
  context_version: number | null
  event_trail: Record<string, unknown>[]
}

export interface ContextVersion {
  thread_id: string
  context_version: number
  triggering_email_id: string
  context: Record<string, unknown>
  changes_from_previous_context?: Record<string, unknown> | null
}

export interface PersonRow {
  id: string
  name?: string | null
  email?: string | null
  aliases?: string[]
  org?: string | null
  org_id?: string | null
  role?: string | null
  last_inbound?: string | null
  last_outbound?: string | null
  open_threads?: string[]
  status?: string | null
  merged_into?: string | null
}

export interface BasisWrapped<T> {
  basis: string
  data: T
}

export interface PersonContext {
  person: PersonRow
  canonical_person_id: string | null
  canonical_resolution_error: string | null
  organization: BasisWrapped<Record<string, unknown> | null>
  emails: BasisWrapped<Record<string, unknown>[]>
  threads: BasisWrapped<ThreadRow[]>
  related_people: BasisWrapped<PersonRow[]>
  other_people_at_org: BasisWrapped<PersonRow[]>
  commitments: Record<string, unknown>[]
  meetings: Record<string, unknown>[]
  follow_ups: Record<string, unknown>[]
  projects: Record<string, unknown>[]
  knowledge: Record<string, unknown>[]
  reply_drafts: Record<string, unknown>[]
  calendar_actions: Record<string, unknown>[]
}

export interface OrganizationRow {
  id: string
  name: string
  domain?: string | null
  aliases?: string[]
}

export interface OrganizationSummary {
  organization: OrganizationRow
  summary: {
    org: string
    people: PersonRow[]
    projects: ProjectRow[]
    related_commitments: Record<string, unknown>[]
    related_follow_ups: Record<string, unknown>[]
    related_meetings: Record<string, unknown>[]
    related_knowledge_items: Record<string, unknown>[]
    relationship_notes: Record<string, string>
  }
}

export interface ProjectRow {
  id: string
  project: string
  entity?: string | null
  status?: string | null
  owner?: string | null
  health?: string | null
  next_milestone?: string | null
  due?: string | null
  org_id?: string | null
  goal_pillar?: string | null
  person_ids?: string[]
}

export interface ProjectSummary {
  project: ProjectRow
  related_commitments: Record<string, unknown>[]
  related_follow_ups: Record<string, unknown>[]
  related_meetings: Record<string, unknown>[]
  related_knowledge_items: Record<string, unknown>[]
  related_people: Record<string, unknown>[]
  relationship_notes: Record<string, string>
}

export interface ProjectFieldsUpdate {
  status?: string | null
  owner?: string | null
  health?: string | null
  next_milestone?: string | null
  due?: string | null
}

export interface OpportunityRow {
  id: string
  name: string
  entity?: string | null
  org_id?: string | null
  description?: string | null
  status?: string | null
  stage?: string | null
  owner?: string | null
  value?: number | null
  currency?: string | null
  expected_close_date?: string | null
  next_action?: string | null
  source_email_ids?: string[]
  project_ids?: string[]
  meeting_ids?: string[]
  person_ids?: string[]
  buying_signals?: string[]
  last_activity_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface OpportunityFieldsUpdate {
  stage?: string | null
  owner?: string | null
  value?: number | null
  currency?: string | null
  expected_close_date?: string | null
  next_action?: string | null
}

export interface CommitmentRow {
  id: string
  what: string
  class?: string | null
  importance?: string | null
  owed_by?: string | null
  owed_to?: string | null
  person_id?: string | null
  org_id?: string | null
  source_record?: string | null
  made_on?: string | null
  committed_date?: string | null
  date_type?: string | null
  status?: string | null
  goal_pillar?: string | null
  project_id?: string | null
  thread_id?: string | null
}

export interface FollowUpRow {
  id: string
  what?: string | null
  person_name?: string | null
  org_name?: string | null
  commitment_id: string
  thread_id?: string | null
  person_id?: string | null
  org_id?: string | null
  status?: string | null
  audience?: string | null
  follow_up_earliest_at?: string | null
  follow_up_latest_at?: string | null
}

export interface MeetingRow {
  id: string
  title?: string | null
  date?: string | null
  attendees?: string[]
  person_ids?: string[]
  org_id?: string | null
  project_or_pillar?: string | null
  actions_raised?: string[]
  actionable?: boolean | null
  thread_id?: string | null
}

export interface MeetingBrief {
  meeting: Record<string, unknown>
  classification: string
  canonical_attendee_ids: string[]
  attendee_contexts: Record<string, unknown>[]
  attendee_resolution_notes: string[]
  organization_context: Record<string, unknown> | null
  project_context: Record<string, unknown> | null
  thread_context: Record<string, unknown> | null
  previous_meetings: Record<string, unknown>[]
  open_commitments: Record<string, unknown>[]
  relevant_follow_ups: Record<string, unknown>[]
  relevant_knowledge: Record<string, unknown>[]
  existing_reply_drafts: Record<string, unknown>[]
  evidence: Record<string, unknown>[]
}

export interface PersonalItemRow {
  id: string
  type?: string | null
  description?: string | null
  date_or_deadline?: string | null
  status?: string | null
  sender_email?: string | null
}

export interface KnowledgeItemRow {
  subject_key: string
  predicate: string
  current_value: string
  basis?: string | null
  confidence?: number | null
  history?: Record<string, unknown>[]
  thread_id?: string | null
}

export interface ReplyDraftContent {
  subject: string
  body: string
}

export type ReplyDraftStatus =
  | 'no_reply_required'
  | 'awaiting_approval'
  | 'approved'
  | 'edited'
  | 'rejected'
  | 'cancelled'
  | 'simulated_sent'
  | 'sent'

export interface ReplyDraftRow {
  reply_id: string
  thread_id: string
  source_email_id: string
  status: ReplyDraftStatus
  draft: ReplyDraftContent
  person_id?: string | null
  org_id?: string | null
  created_by?: string
  created_at?: string | null
  recipient?: string | null
  org_name?: string | null
}

export type CalendarActionStatus =
  | 'pending'
  | 'awaiting_approval'
  | 'approved'
  | 'scheduled'
  | 'failed'
  | 'rejected'
  | 'needs_clarification'

export interface CalendarEventTime {
  start?: string | null
  end?: string | null
}

export interface CalendarEvent {
  title: string
  time?: CalendarEventTime
  attendees?: unknown[]
}

export interface CalendarActionRow {
  thread_id: string
  meeting_fingerprint: string
  status: CalendarActionStatus
  event: CalendarEvent
  reason?: string | null
  person_id?: string | null
  org_id?: string | null
  meeting_id?: string | null
  person_name?: string | null
  org_name?: string | null
}

export interface OverviewMetrics {
  emails_processed: number
  threads: number
  knowledge_items: number
  pending_replies: number
  pending_calendar_actions: number
  failures: number
}

export interface OverviewAttention {
  pending_replies: ReplyDraftRow[]
  overdue_follow_ups: FollowUpRow[]
  commitments_due: CommitmentRow[]
  upcoming_meetings: MeetingRow[]
  active_projects: ProjectRow[]
}

export interface Overview {
  metrics: OverviewMetrics
  attention: OverviewAttention
}

export interface PageParams {
  limit?: number
  offset?: number
}
