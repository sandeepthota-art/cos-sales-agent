import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { OpportunityDetailPage } from '../OpportunityDetailPage'

const OPPORTUNITY_BODY = {
  id: 'OPP-001',
  name: 'Acme Deal',
  entity: 'Acme',
  org_id: 'ORG-001',
  status: 'open',
  stage: 'Discovery',
  owner: 'Sandeep',
  value: 50000,
  currency: 'USD',
  expected_close_date: '2026-03-01',
  next_action: 'Send proposal',
  project_ids: ['PRJ-001'],
  person_ids: ['PER-001'],
  meeting_ids: [],
  buying_signals: [],
}

const UPDATED_OPPORTUNITY = { ...OPPORTUNITY_BODY, stage: 'Negotiation' }

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/opportunities/OPP-001']}>
      <AuthProvider>
        <Routes>
          <Route path="/opportunities/:opportunityId" element={<OpportunityDetailPage />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('OpportunityDetailPage', () => {
  it('loads the opportunity and saves an edited stage via PATCH', async () => {
    const fetchMock = installMockFetch((url, init) => {
      if (url.includes('/commitments')) return { status: 200, body: [] }
      if (url.includes('/opportunities/OPP-001') && init?.method === 'PATCH') {
        return { status: 200, body: UPDATED_OPPORTUNITY }
      }
      if (url.includes('/opportunities/OPP-001')) return { status: 200, body: OPPORTUNITY_BODY }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Acme Deal' })).toBeInTheDocument()

    const stageInput = screen.getByLabelText('Stage') as HTMLInputElement
    fireEvent.change(stageInput, { target: { value: 'Negotiation' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/opportunities/OPP-001'),
        expect.objectContaining({ method: 'PATCH' }),
      )
    })
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })
})
