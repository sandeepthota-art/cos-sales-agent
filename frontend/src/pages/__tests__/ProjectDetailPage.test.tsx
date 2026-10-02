import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { ProjectDetailPage } from '../ProjectDetailPage'

const PROJECT_SUMMARY_BODY = {
  project: {
    id: 'PRJ-001',
    project: 'Acme Rollout',
    entity: 'Acme',
    status: 'open',
    owner: 'Sandeep',
    health: 'on_track',
    next_milestone: 'Kickoff',
    due: '2026-02-01',
  },
  related_commitments: [],
  related_follow_ups: [],
  related_meetings: [],
  related_knowledge_items: [],
  related_people: [],
  relationship_notes: {},
}

const UPDATED_PROJECT = { ...PROJECT_SUMMARY_BODY.project, owner: 'New Owner' }

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/projects/PRJ-001']}>
      <AuthProvider>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('ProjectDetailPage', () => {
  it('loads the project and saves an edited field via PATCH', async () => {
    const fetchMock = installMockFetch((url, init) => {
      if (url.includes('/commitments')) return { status: 200, body: [] }
      if (url.includes('/projects/PRJ-001') && init?.method === 'PATCH') {
        return { status: 200, body: UPDATED_PROJECT }
      }
      if (url.includes('/projects/PRJ-001')) return { status: 200, body: PROJECT_SUMMARY_BODY }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Acme Rollout' })).toBeInTheDocument()

    const ownerInput = screen.getByLabelText('Owner') as HTMLInputElement
    fireEvent.change(ownerInput, { target: { value: 'New Owner' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/projects/PRJ-001'),
        expect.objectContaining({ method: 'PATCH' }),
      )
    })
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })
})
