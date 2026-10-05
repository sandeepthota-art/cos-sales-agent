import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { MeetingBriefPage } from '../MeetingBriefPage'

const BRIEF_BODY = {
  meeting: {
    id: 'MTG-001',
    date: '2026-10-06T03:24:39Z',
    attendees: ['Sandeep Thota', 'Vijender'],
    project_or_pillar: 'Sales',
    actionable: true,
    actions_raised: ['Prepare agenda on available trainings'],
    agenda_written: false,
    thread_id: 'THR-009',
  },
  classification: 'Sales',
  canonical_attendee_ids: ['PER-001', 'PER-002'],
  attendee_contexts: [
    {
      person: {
        id: 'PER-001', name: 'Sandeep Thota', email: 'sandeep.thota@databeat.io',
        role: 'Databeat (ATT Team / AI Engineering team contact)',
        profile_summary: 'Sandeep Thota, Databeat. Sends multiple staffing and training requests.',
        recent_context: 'Training meeting set for 10am IST Oct 6.',
      },
      canonical_person_id: 'PER-001', canonical_resolution_error: null,
      organization: { basis: 'canonical_id', data: { id: 'ORG-001', name: 'Databeat' } },
      emails: { basis: 'canonical_id', data: [] }, threads: { basis: 'canonical_id', data: [] },
      related_people: { basis: 'canonical_id_shared_thread', data: [] },
      other_people_at_org: { basis: 'canonical_id', data: [] },
      commitments: [], meetings: [], follow_ups: [], projects: [], knowledge: [],
      reply_drafts: [], calendar_actions: [],
    },
  ],
  attendee_resolution_notes: [],
  organization_context: {
    organization: {
      id: 'ORG-001', name: 'Databeat', domain: 'databeat.io',
      industry: 'AdTech / Programmatic Advertising Analytics',
      description: 'Advanced analytics and programmatic advertising optimization for AdTech partners.',
    },
  },
  project_context: null,
  thread_context: {
    thread: {
      thread_id: 'THR-009', normalized_subject: 'Training schedule', participant_emails: [],
      message_count: 4, last_message_at: '2026-10-05T03:24:39Z',
    },
  },
  previous_meetings: [],
  open_commitments: [
    {
      id: 'CMT-006', what: 'Prepare an agenda for the next meeting on available trainings',
      owed_by: 'Vijender', owed_to: 'Sandeep Thota', committed_date: '2026-10-06T03:14:55Z',
      status: 'open', class: 'mine', goal_pillar: 'Sales', made_on: '2026-10-05T03:14:55Z',
      source_record: 'EML-009', thread_id: 'THR-009',
    },
  ],
  relevant_follow_ups: [],
  relevant_knowledge: [
    {
      knowledge_id: 'KNOW-001',
      current_value: "Agenda for tomorrow's meeting on available trainings",
    },
  ],
  existing_reply_drafts: [],
  evidence: [],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/meetings/MTG-001/brief']}>
      <AuthProvider>
        <Routes>
          <Route path="/meetings/:meetingId/brief" element={<MeetingBriefPage />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('MeetingBriefPage', () => {
  it('renders the simplified, human-readable brief', async () => {
    installMockFetch((url) => {
      if (url.includes('/meetings/MTG-001/brief')) return { status: 200, body: BRIEF_BODY }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Meeting Brief' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Sandeep Thota' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Databeat' })).toBeInTheDocument()
    expect(screen.getByText('AdTech / Programmatic Advertising Analytics')).toBeInTheDocument()
    expect(screen.getByText(/Prepare an agenda for the next meeting/)).toBeInTheDocument()
    expect(screen.getByText(/Agenda for tomorrow's meeting on available trainings/)).toBeInTheDocument()
  })

  it('does not render raw ids, fingerprints, or provenance fields', async () => {
    installMockFetch((url) => {
      if (url.includes('/meetings/MTG-001/brief')) return { status: 200, body: BRIEF_BODY }
      return { status: 404 }
    })

    renderPage()
    await screen.findByRole('heading', { name: 'Meeting Brief' })

    expect(screen.queryByText('THR-009')).not.toBeInTheDocument()
    expect(screen.queryByText('CMT-006')).not.toBeInTheDocument()
    expect(screen.queryByText(/Source Record/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Confidence/)).not.toBeInTheDocument()
    expect(screen.queryByText(/EML-009/)).not.toBeInTheDocument()
    expect(screen.queryByText('Subject Key')).not.toBeInTheDocument()
  })
})
