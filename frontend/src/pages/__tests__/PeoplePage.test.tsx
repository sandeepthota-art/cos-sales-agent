import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { PeoplePage } from '../PeoplePage'

describe('PeoplePage', () => {
  it('shows a one-line profile teaser per person', async () => {
    installMockFetch((url) => {
      if (url.includes('/people')) {
        return {
          status: 200,
          body: [
            {
              id: 'PER-001', name: 'Vijender', email: 'vijender@alumnx.com', company: 'Alumnx AI Labs',
              role: 'AI Consultant', status: 'active',
              profile_summary: 'Vijender is an AI Training and AI Consulting professional.',
              key_topics: ['AI Engineer hiring'],
            },
          ],
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter>
        <AuthProvider>
          <PeoplePage />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByText('Vijender')).toBeInTheDocument()
    expect(screen.getByText(/AI Training and AI Consulting professional/)).toBeInTheDocument()
  })

  it('truncates a long profile summary to an 80-character teaser with an ellipsis', async () => {
    const longSummary =
      'Vijender is an AI Training and AI Consulting professional at Alumnx AI Labs, ' +
      'where he works primarily across AI HR consulting, AI training, and AI software solutions.'
    expect(longSummary.length).toBeGreaterThan(80)
    const expectedTeaser = `${longSummary.slice(0, 80)}…`

    installMockFetch((url) => {
      if (url.includes('/people')) {
        return {
          status: 200,
          body: [
            {
              id: 'PER-002', name: 'Rahul', email: 'rahul@alumnx.com', company: 'Alumnx AI Labs',
              role: 'AI Consultant', status: 'active',
              profile_summary: longSummary,
              key_topics: [],
            },
          ],
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter>
        <AuthProvider>
          <PeoplePage />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByText('Rahul')).toBeInTheDocument()
    expect(screen.getByText(expectedTeaser)).toBeInTheDocument()
    expect(screen.queryByText(longSummary)).not.toBeInTheDocument()
  })
})
