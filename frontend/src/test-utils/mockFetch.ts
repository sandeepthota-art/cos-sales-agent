import { vi } from 'vitest'

export interface MockResponseSpec {
  status?: number
  body?: unknown
}

export type FetchHandler = (url: string, init?: RequestInit) => MockResponseSpec

/** Installs a fake `global.fetch` driven by a handler function, so API-layer
 * tests never touch a real network/backend. Returns the vi.fn() so a test
 * can assert on call arguments if it needs to. */
export function installMockFetch(handler: FetchHandler): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString()
    const { status = 200, body } = handler(url, init)
    return new Response(body === undefined ? undefined : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
