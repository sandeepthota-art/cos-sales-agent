import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { getAuthConfig, type AuthConfig } from '../api/auth'
import { ApiError } from '../api/client'

interface LocationState {
  from?: { pathname: string }
}

interface GoogleCredentialResponse {
  credential: string
}

interface GoogleAccountsId {
  initialize: (config: {
    client_id: string
    callback: (response: GoogleCredentialResponse) => void
  }) => void
  renderButton: (
    parent: HTMLElement,
    options: { theme: string; size: string; width?: number },
  ) => void
}

declare global {
  interface Window {
    google?: { accounts: { id: GoogleAccountsId } }
  }
}

const GOOGLE_SCRIPT_ID = 'google-identity-services'
const GOOGLE_SCRIPT_SRC = 'https://accounts.google.com/gsi/client'

function GoogleSignInButton({ clientId }: { clientId: string }) {
  const { loginWithGoogle } = useAuth()
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)
  const buttonRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    async function handleCredential(response: GoogleCredentialResponse) {
      setError(null)
      try {
        await loginWithGoogle(response.credential)
        navigate('/', { replace: true })
      } catch (err: unknown) {
        setError(err instanceof ApiError ? err.message : 'Unable to sign in')
      }
    }

    function renderButton() {
      if (!buttonRef.current || !window.google) return
      window.google.accounts.id.initialize({ client_id: clientId, callback: handleCredential })
      window.google.accounts.id.renderButton(buttonRef.current, { theme: 'outline', size: 'large', width: 280 })
    }

    if (window.google) {
      renderButton()
      return
    }

    // Loaded on demand (rather than a static <script> tag in index.html) so
    // there's no race between this script's own load and React mounting --
    // window.google is guaranteed present by the time renderButton runs.
    let script = document.getElementById(GOOGLE_SCRIPT_ID) as HTMLScriptElement | null
    if (!script) {
      script = document.createElement('script')
      script.id = GOOGLE_SCRIPT_ID
      script.src = GOOGLE_SCRIPT_SRC
      script.async = true
      document.head.appendChild(script)
    }
    script.addEventListener('load', renderButton)
    return () => script?.removeEventListener('load', renderButton)
  }, [clientId, loginWithGoogle, navigate])

  return (
    <>
      <div ref={buttonRef} style={{ display: 'flex', justifyContent: 'center', marginTop: '1.5rem' }} />
      {error && (
        <p className="login-card__error" role="alert">
          {error}
        </p>
      )}
    </>
  )
}

function PasswordSignInForm() {
  const { loginWithPassword } = useAuth()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await loginWithPassword(password)
      navigate('/', { replace: true })
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : 'Unable to sign in')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoFocus
          required
        />
      </div>
      {error && (
        <p className="login-card__error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="button button--primary" disabled={submitting} style={{ width: '100%' }}>
        {submitting ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  )
}

export function LoginPage() {
  const { status } = useAuth()
  const location = useLocation()
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)

  useEffect(() => {
    getAuthConfig()
      .then(setConfig)
      .catch(() => setConfigError('Unable to load sign-in configuration'))
  }, [])

  if (status === 'authenticated') {
    const state = location.state as LocationState | null
    return <Navigate to={state?.from?.pathname ?? '/'} replace />
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-card__brand">
          <span className="login-card__brand-name">CoS Staff EA Agent</span>
          <span className="login-card__brand-subtitle">Chief of Staff • Executive Assistant</span>
        </div>
        {configError && (
          <p className="login-card__error" role="alert">
            {configError}
          </p>
        )}
        {!configError && !config && (
          <p className="login-card__hint" style={{ textAlign: 'center', marginTop: '1rem' }}>
            Loading sign-in…
          </p>
        )}
        {config?.google_client_id && <GoogleSignInButton clientId={config.google_client_id} />}
        {!config?.google_client_id && config?.password_auth_enabled && <PasswordSignInForm />}
      </div>
    </div>
  )
}
