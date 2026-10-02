import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { installMockFetch } from '../../test-utils/mockFetch'
import { renderWithProviders } from '../../test-utils/renderWithRouter'
import { OverviewPage } from '../OverviewPage'

const OVERVIEW_BODY = {
  metrics: {
    emails_processed: 42,
    threads: 10,
    knowledge_items: 5,
    pending_replies: 1,
    pending_calendar_actions: 2,
    failures: 0,
  },
  attention: {
    pending_replies: [],
    overdue_follow_ups: [],
    commitments_due: [],
    upcoming_meetings: [],
    active_projects: [{ id: 'PRJ-001', project: 'Acme Rollout', entity: 'Acme', status: 'open', owner: 'Sandeep' }],
  },
}

describe('OverviewPage', () => {
  it('renders metrics and attention sections from the API', async () => {
    installMockFetch((url) => {
      if (url.includes('/overview')) return { status: 200, body: OVERVIEW_BODY }
      if (url.includes('/commitments')) return { status: 200, body: [] }
      return { status: 404 }
    })

    renderWithProviders(<OverviewPage />)

    expect(await screen.findByText('42')).toBeInTheDocument()
    expect(screen.getByText('Emails Processed')).toBeInTheDocument()
    expect(screen.getByText('Acme Rollout')).toBeInTheDocument()
  })

  it('shows an error state when the overview request fails', async () => {
    installMockFetch((url) => {
      if (url.includes('/overview')) return { status: 500, body: { detail: 'Internal error' } }
      if (url.includes('/commitments')) return { status: 200, body: [] }
      return { status: 404 }
    })

    renderWithProviders(<OverviewPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal error')
  })
})
