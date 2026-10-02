import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { PersonDetailPage } from '../PersonDetailPage'

const PERSON_CONTEXT_BODY = {
  person: {
    id: 'PER-001',
    name: 'Ashok Kumar',
    email: 'ashok@acme.com',
    org: 'Acme',
    role: 'CTO',
    status: 'active',
  },
  canonical_person_id: 'PER-001',
  canonical_resolution_error: null,
  organization: { basis: 'canonical_id', data: { id: 'ORG-001', name: 'Acme Corp' } },
  emails: { basis: 'canonical_id', data: [] },
  threads: { basis: 'canonical_id', data: [] },
  related_people: { basis: 'canonical_id_shared_thread', data: [] },
  other_people_at_org: { basis: 'canonical_id', data: [] },
  commitments: [],
  meetings: [],
  follow_ups: [],
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
  it('renders the person 360 view', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-001')) return { status: 200, body: PERSON_CONTEXT_BODY }
      if (url.includes('/commitments')) return { status: 200, body: [] }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Ashok Kumar' })).toBeInTheDocument()
    expect(screen.getByText('ashok@acme.com', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Acme Corp')).toBeInTheDocument()
  })
})
