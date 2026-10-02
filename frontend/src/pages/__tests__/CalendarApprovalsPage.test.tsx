import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { installMockFetch } from '../../test-utils/mockFetch'
import { renderWithProviders } from '../../test-utils/renderWithRouter'
import { CalendarApprovalsPage } from '../CalendarApprovalsPage'

const ACTION = {
  thread_id: 'THR-001',
  meeting_fingerprint: 'FP-001',
  status: 'awaiting_approval',
  event: { title: 'Kickoff Call', time: { start: '2026-02-01T10:00:00Z', end: '2026-02-01T10:30:00Z' } },
  person_id: 'PER-001',
  org_id: 'ORG-001',
  person_name: 'Ashok Kumar',
  org_name: 'Acme Corp',
}

describe('CalendarApprovalsPage', () => {
  it('approves a calendar action and reloads the list', async () => {
    let listCallCount = 0
    const fetchMock = installMockFetch((url) => {
      if (url.includes('/calendar-actions/THR-001/FP-001/approve')) {
        return { status: 200, body: { ...ACTION, status: 'scheduled' } }
      }
      if (url.includes('/calendar-actions')) {
        listCallCount += 1
        return { status: 200, body: listCallCount === 1 ? [ACTION] : [] }
      }
      if (url.includes('/commitments')) return { status: 200, body: [] }
      return { status: 404 }
    })

    renderWithProviders(<CalendarApprovalsPage />)

    expect(await screen.findByText('Kickoff Call')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/calendar-actions/THR-001/FP-001/approve'),
        expect.objectContaining({ method: 'POST' }),
      )
    })

    expect(await screen.findByText('No calendar actions awaiting approval')).toBeInTheDocument()
  })
})
