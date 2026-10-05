import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { PersonDetailPage } from '../PersonDetailPage'

const PERSON_CONTEXT_BODY = {
  person: {
    id: 'PER-001',
    name: 'Vijender',
    email: 'vijender@alumnx.com',
    org: 'Alumnx AI Labs',
    role: 'AI Training and AI Consulting professional',
    status: 'active',
    profile_summary:
      'Vijender is an AI Training and AI Consulting professional at Alumnx AI Labs, where he works primarily across AI HR consulting, AI training, and AI software solutions.',
    recent_context:
      'Vijender has discussed AI Engineer requirements with Databeat, including candidate profiles and engagement duration.',
    key_topics: ['AI Engineer hiring', 'AI training programs'],
  },
  canonical_person_id: 'PER-001',
  canonical_resolution_error: null,
  organization: { basis: 'canonical_id', data: { id: 'ORG-001', name: 'Alumnx AI Labs' } },
  emails: { basis: 'canonical_id', data: [] },
  threads: { basis: 'canonical_id', data: [] },
  related_people: { basis: 'canonical_id_shared_thread', data: [] },
  other_people_at_org: { basis: 'canonical_id', data: [] },
  commitments: [{ id: 'CMT-001', what: 'send pricing' }],
  meetings: [{ id: 'MTG-001' }],
  follow_ups: [{ id: 'FUP-001' }],
  projects: [],
  knowledge: [],
  reply_drafts: [],
  calendar_actions: [],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/people/PER-001']}>
      <AuthProvider>
        <Routes>
          <Route path="/people/:personId" element={<PersonDetailPage />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('PersonDetailPage', () => {
  it('renders the composed profile and recent context', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-001')) return { status: 200, body: PERSON_CONTEXT_BODY }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Vijender' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Alumnx AI Labs' })).toBeInTheDocument()
    expect(screen.getByText(/AI Training and AI Consulting professional at Alumnx AI Labs/)).toBeInTheDocument()
    expect(screen.getByText(/discussed AI Engineer requirements with Databeat/)).toBeInTheDocument()
    expect(screen.getByText(/AI Engineer hiring/)).toBeInTheDocument()
  })

  it('does not render raw commitments/meetings/follow-up data', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-001')) return { status: 200, body: PERSON_CONTEXT_BODY }
      return { status: 404 }
    })

    renderPage()
    await screen.findByRole('heading', { name: 'Vijender' })

    expect(screen.queryByText('CMT-001')).not.toBeInTheDocument()
    expect(screen.queryByText('MTG-001')).not.toBeInTheDocument()
    expect(screen.queryByText('FUP-001')).not.toBeInTheDocument()
    expect(screen.queryByText('send pricing')).not.toBeInTheDocument()
  })

  it('renders a placeholder when no profile has been composed yet', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-002')) {
        return {
          status: 200,
          body: {
            ...PERSON_CONTEXT_BODY,
            person: {
              id: 'PER-002', name: 'New Contact', email: 'new@newco.com', org: null,
              role: null, status: 'active', profile_summary: null, recent_context: null, key_topics: [],
            },
          },
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter initialEntries={['/people/PER-002']}>
        <AuthProvider>
          <Routes>
            <Route path="/people/:personId" element={<PersonDetailPage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'New Contact' })).toBeInTheDocument()
    expect(screen.getByText(/No profile yet/)).toBeInTheDocument()
  })
})
