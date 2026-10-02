import { apiGet, buildQuery } from './client'
import type { MeetingBrief, MeetingRow, PageParams } from './types'

export function listMeetings(params: PageParams = {}): Promise<MeetingRow[]> {
  return apiGet(`/meetings${buildQuery(params)}`)
}

export function getMeetingBrief(meetingId: string): Promise<MeetingBrief> {
  return apiGet(`/meetings/${encodeURIComponent(meetingId)}/brief`)
}
