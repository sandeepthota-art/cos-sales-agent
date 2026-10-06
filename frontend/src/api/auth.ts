import { apiGet, apiPost } from './client'

export interface AuthConfig {
  google_client_id: string | null
  password_auth_enabled: boolean
}

export function getAuthConfig(): Promise<AuthConfig> {
  return apiGet('/auth/config')
}

export function loginWithGoogle(credential: string): Promise<{ ok: boolean }> {
  return apiPost('/auth/login', { credential })
}

export function loginWithPassword(password: string): Promise<{ ok: boolean }> {
  return apiPost('/auth/login/password', { password })
}

export function logout(): Promise<{ ok: boolean }> {
  return apiPost('/auth/logout')
}
