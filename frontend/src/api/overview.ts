import { apiGet } from './client'
import type { Overview } from './types'

export function getOverview(): Promise<Overview> {
  return apiGet('/overview')
}
