import { apiGet, apiPost } from './client'
import type { CalendarActionRow } from './types'

export function listCalendarActions(): Promise<CalendarActionRow[]> {
  return apiGet('/calendar-actions')
}

export function approveCalendarAction(
  threadId: string,
  meetingFingerprint: string,
): Promise<CalendarActionRow> {
  return apiPost(
    `/calendar-actions/${encodeURIComponent(threadId)}/${encodeURIComponent(meetingFingerprint)}/approve`,
  )
}

export function ignoreCalendarAction(
  threadId: string,
  meetingFingerprint: string,
): Promise<CalendarActionRow> {
  return apiPost(
    `/calendar-actions/${encodeURIComponent(threadId)}/${encodeURIComponent(meetingFingerprint)}/ignore`,
  )
}
