import { apiPost } from './client'

export function login(password: string): Promise<{ ok: boolean }> {
  return apiPost('/auth/login', { password })
}

export function logout(): Promise<{ ok: boolean }> {
  return apiPost('/auth/logout')
}
