// Centralized fetch wrapper -- the ONLY place that calls fetch() against the
// FastAPI backend. No component or page ever constructs a request directly
// (see docs/REACT_MIGRATION_PLAN.md's API-client-layer requirement).
//
// Auth is a session cookie (httpOnly, set by the backend) -- credentials are
// never read or stored in JS. A 401 anywhere means "not authenticated" (first
// load or an expired session); this dispatches a window event that
// AuthContext listens for, so any page's failed request can flip the whole
// app back to the login screen without each call site handling it.

const API_BASE = '/api/v1'

export const UNAUTHORIZED_EVENT = 'cos:unauthorized'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

interface ErrorBody {
  detail?: string
}

function isErrorBody(value: unknown): value is ErrorBody {
  return typeof value === 'object' && value !== null && 'detail' in value
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (isErrorBody(body) && typeof body.detail === 'string') {
      return body.detail
    }
  } catch {
    // Response body wasn't JSON -- fall through to the status text.
  }
  return response.statusText || `Request failed with status ${response.status}`
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...init.headers,
    },
    ...init,
  })

  if (!response.ok) {
    const message = await parseErrorMessage(response)
    if (response.status === 401) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    throw new ApiError(response.status, message)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
}

export function buildQuery<T extends object>(params: T): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (typeof value === 'string' || typeof value === 'number') {
      search.set(key, String(value))
    }
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}
