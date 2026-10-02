import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { login as loginRequest, logout as logoutRequest } from '../api/auth'
import { apiGet, UNAUTHORIZED_EVENT } from '../api/client'

export type AuthStatus = 'checking' | 'authenticated' | 'anonymous'

interface AuthContextValue {
  status: AuthStatus
  login: (password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

interface AuthProviderProps {
  children: ReactNode
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [status, setStatus] = useState<AuthStatus>('checking')

  useEffect(() => {
    const onUnauthorized = () => setStatus('anonymous')
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  useEffect(() => {
    // No dedicated "whoami" endpoint exists -- a cheap, already-existing
    // protected route doubles as the session probe. require_auth bypasses
    // entirely when api_password_hash is unset, so this also succeeds for a
    // deployment that never configured auth at all.
    apiGet('/commitments?limit=1')
      .then(() => setStatus('authenticated'))
      .catch(() => setStatus('anonymous'))
  }, [])

  const login = useCallback(async (password: string) => {
    await loginRequest(password)
    setStatus('authenticated')
  }, [])

  const logout = useCallback(async () => {
    await logoutRequest()
    setStatus('anonymous')
  }, [])

  return <AuthContext.Provider value={{ status, login, logout }}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within an AuthProvider')
  return context
}
