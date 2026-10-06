import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installMockFetch } from '../../test-utils/mockFetch'
import { renderWithProviders } from '../../test-utils/renderWithRouter'
import { LoginPage } from '../LoginPage'

const CLIENT_ID = 'test-client-id.apps.googleusercontent.com'

describe('LoginPage (Google SSO mode)', () => {
  let credentialCallback: ((response: { credential: string }) => void) | null

  beforeEach(() => {
    credentialCallback = null
    // Stands in for the real Google Identity Services script -- already
    // present on `window` so LoginPage's effect renders the button directly
    // instead of injecting/waiting on the real <script src="accounts.google.com/..."> tag.
    window.google = {
      accounts: {
        id: {
          initialize: vi.fn(({ callback }) => {
            credentialCallback = callback
          }),
          renderButton: vi.fn(),
        },
      },
    }
  })

  afterEach(() => {
    delete (window as { google?: unknown }).google
  })

  it('signs in successfully once Google returns a credential', async () => {
    installMockFetch((url) => {
      if (url.includes('/auth/config'))
        return { status: 200, body: { google_client_id: CLIENT_ID, password_auth_enabled: false } }
      if (url.includes('/auth/login')) return { status: 200, body: { ok: true } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    await waitFor(() => expect(credentialCallback).not.toBeNull())

    await act(async () => {
      credentialCallback?.({ credential: 'a-real-looking-jwt' })
    })

    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('shows an error message when Google sign-in is rejected', async () => {
    installMockFetch((url) => {
      if (url.includes('/auth/config'))
        return { status: 200, body: { google_client_id: CLIENT_ID, password_auth_enabled: false } }
      if (url.includes('/auth/login'))
        return { status: 403, body: { detail: 'This Google account is not authorized for this dashboard' } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    await waitFor(() => expect(credentialCallback).not.toBeNull())

    await act(async () => {
      credentialCallback?.({ credential: 'a-real-looking-jwt' })
    })

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This Google account is not authorized for this dashboard',
    )
  })
})

describe('LoginPage (password mode -- e.g. a testing deployment without Google SSO set up)', () => {
  it('submits the password and signs in successfully', async () => {
    const fetchMock = installMockFetch((url) => {
      if (url.includes('/auth/config'))
        return { status: 200, body: { google_client_id: null, password_auth_enabled: true } }
      if (url.includes('/auth/login/password')) return { status: 200, body: { ok: true } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    await screen.findByLabelText('Password')
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/auth/login/password'),
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('shows an error message when the password is incorrect', async () => {
    installMockFetch((url) => {
      if (url.includes('/auth/config'))
        return { status: 200, body: { google_client_id: null, password_auth_enabled: true } }
      if (url.includes('/auth/login/password')) return { status: 401, body: { detail: 'Incorrect password' } }
      if (url.includes('/commitments')) return { status: 401, body: { detail: 'Not authenticated' } }
      return { status: 404 }
    })

    renderWithProviders(<LoginPage />, ['/login'])

    await screen.findByLabelText('Password')
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect password')
  })
})
