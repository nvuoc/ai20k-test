import type { ActionInput, ChatSnapshot } from './api.generated'

export type {
  PublicCandidate as Candidate, PublicBooking as Booking, TripSummary as Summary,
  AssistantResponse as Response, ChatSnapshot as Snapshot, Bootstrap,
  Receipt, AckInput, MessageInput, SessionInput, ActionInput,
} from './api.generated'
export type Event = ChatSnapshot['events'][number]
export type Action = ActionInput['action']

export async function api<T>(path: string, data?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: data === undefined ? {} : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.message || body.detail || `Lỗi kết nối (${response.status})`)
  }
  return response.json() as Promise<T>
}
