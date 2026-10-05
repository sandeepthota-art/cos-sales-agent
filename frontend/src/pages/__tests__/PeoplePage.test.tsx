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
})
