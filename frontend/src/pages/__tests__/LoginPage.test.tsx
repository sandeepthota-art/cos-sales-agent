import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { installMockFetch } from '../../test-utils/mockFetch'
import { renderWithProviders } from '../../test-utils/renderWithRouter'
import { LoginPage } from '../LoginPage'

describe('LoginPage', () => {
  it('submits the password and signs in successfully', async () => {
    const fetchMock = installMockFetch((url) => {
      if (url.includes('/auth/login')) return { status: 200, body: { ok: true } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/auth/login'),
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('shows an error message when the password is incorrect', async () => {
    installMockFetch((url) => {
      if (url.includes('/auth/login')) return { status: 401, body: { detail: 'Incorrect password' } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect password')
  })
})
