import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { installMockFetch } from '../../test-utils/mockFetch'
import { renderWithProviders } from '../../test-utils/renderWithRouter'
import { ReplyApprovalsPage } from '../ReplyApprovalsPage'

const DRAFT = {
  reply_id: 'RPL-001',
  thread_id: 'THR-001',
  source_email_id: 'EML-001',
  status: 'awaiting_approval',
  draft: { subject: 'Re: Proposal', body: 'Thanks for reaching out.' },
  person_id: 'PER-001',
  org_id: 'ORG-001',
  recipient: 'Ashok Kumar',
  org_name: 'Acme Corp',
}

describe('ReplyApprovalsPage', () => {
  it('approves a reply draft and reloads the list', async () => {
    let listCallCount = 0
    const fetchMock = installMockFetch((url) => {
      if (url.includes('/reply-drafts/RPL-001/approve')) {
        return { status: 200, body: { ...DRAFT, status: 'simulated_sent' } }
      }
      if (url.includes('/reply-drafts')) {
        listCallCount += 1
        return { status: 200, body: listCallCount === 1 ? [DRAFT] : [] }
      }
      if (url.includes('/commitments')) return { status: 200, body: [] }
      return { status: 404 }
    })

    renderWithProviders(<ReplyApprovalsPage />)

    expect(await screen.findByText('Re: Proposal')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/reply-drafts/RPL-001/approve'),
        expect.objectContaining({ method: 'POST' }),
      )
    })

    expect(await screen.findByText('No reply drafts awaiting approval')).toBeInTheDocument()
  })
})
