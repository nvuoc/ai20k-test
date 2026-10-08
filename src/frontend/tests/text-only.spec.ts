import { expect, test, type Page } from '@playwright/test'
import type { Bootstrap, Event, MessageInput, Response, Snapshot } from '../src/api'

const response: Response = {
  response_id: 'text-response-1', generation: 1,
  text: 'Bạn muốn đến địa điểm nào?\n1. Cổng A\n2. Cổng B\nHãy nhắn số thứ tự hoặc tên địa điểm.',
  action: 'select_candidate', focus: 'destination',
  candidates: [{ candidate_id: 'candidate-a', candidate_set_id: 'candidates-1', target: 'destination', ordinal: 1, label: 'Dữ liệu lựa chọn chỉ dành cho server' }],
  summary: {
    pickup: 'Dữ liệu tóm tắt chỉ dành cho server', destination: 'Cổng A', pickup_time: 'Đi ngay',
    passengers: 1, vehicle_type: 'oto_4_cho', vehicle_label: 'Ô tô 4 chỗ', contact_phone: '0901234567',
    contact_name: null, pickup_note: null, luggage: null, payment_method: null, stops: null,
    special_requests: null, fare: 46000, currency: 'VND', quote_expires_at: 2000000000,
    booking_revision: 1, snapshot_fingerprint: 'fingerprint', prompt_id: 'prompt-1',
  },
  presentation: { contract_version: 'chat-presentation-2', response_id: 'text-response-1', generation: 1 },
  booking_status: 'collecting_info', reason: null,
  booking: { booking_id: 'SBX-TEXT', status: 'booked', provider_status: 'booked', provider: 'sandbox' },
  inquiry: {
    inquiry_id: 'inquiry-1', revision: 1, origin: 'Cổng A', destination: 'Cổng B',
    vehicle: 'oto_4_cho', status: 'ready', expires_at: 2000000000,
    route_fingerprint: 'route-1', can_use_route: true, booking_revision: 1,
  },
}

async function mockTextChat(page: Page, failFirstMessage = false) {
  await page.addInitScript(() => localStorage.setItem('di-cung-session', 'text-session'))
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  const bootstrap: Bootstrap = {
    api_version: 'chat-api-2', mode: 'sandbox', profile: 'test', llm_provider: 'groq',
    model: 'openai/gpt-oss-120b', maps_provider: 'fixture', booking_provider: 'sandbox', gemini_rpm: 15,
    capabilities: { asap: true, scheduled: false, multi_stop: false },
    vehicles: [{ code: 'oto_7_cho', label: 'Ô tô 7 chỗ', max_passengers: 6 }],
  }
  const history: Event[] = [{
    cursor: 1, event_id: 'text-event-1', occurred_at: '2026-10-03T08:00:00+07:00',
    type: 'assistant_response', payload: response,
  }]
  const state: Snapshot = {
    api_version: 'chat-api-3', architecture_version: 'architecture-fixed-1', customer_name: 'An', customer_phone: '0901234567', session_id: 'text-session', events: history,
    next_cursor: 1, current_cursor: 1, has_more: false, pending_count: 0,
    booking_status: 'collecting_info', draft_id: 'text-draft', active_response: response,
    booking: response.booking,
  }
  const messages: MessageInput[] = []
  const actions: string[] = []
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    let body: unknown = state
    if (url.pathname === '/api/bootstrap') body = bootstrap
    else if (url.pathname.endsWith('/delivery-acks')) body = { status: 'acknowledged' }
    else if (url.pathname.endsWith('/actions')) {
      actions.push(route.request().postData() || '')
      await route.fulfill({ status: 400, json: { message: 'The frontend must send text messages only' } })
      return
    } else if (url.pathname.endsWith('/messages')) {
      const message = route.request().postDataJSON() as MessageInput
      messages.push(message)
      if (failFirstMessage && messages.length === 1) {
        await route.fulfill({ status: 503, json: { message: 'Mất kết nối tạm thời' } })
        return
      }
      const next: Response = {
        ...response, response_id: 'text-response-2', generation: 2,
        text: 'Mình đã nhận lựa chọn bằng văn bản của bạn.', candidates: [], summary: null, inquiry: null,
      }
      history.push(
        { cursor: 2, event_id: 'text-event-2', occurred_at: '2026-10-03T08:01:00+07:00', type: 'message_received', payload: { text: message.text, message_id: message.client_message_id, event_id: 'text-event-2', role: 'user' } },
        { cursor: 3, event_id: 'text-event-3', occurred_at: '2026-10-03T08:01:01+07:00', type: 'assistant_response', payload: next },
      )
      state.next_cursor = state.current_cursor = 3
      state.active_response = next
      body = { status: 'received' }
    } else if (url.pathname.endsWith('/updates')) {
      const after = Number(url.searchParams.get('after_cursor'))
      body = { ...state, events: history.filter(event => event.cursor > after) }
    }
    await route.fulfill({ json: body })
  })
  return { messages, actions }
}

test('rich response metadata produces only text replies and no selectable chat UI', async ({ page }) => {
  const chat = await mockTextChat(page)
  await page.goto('/')
  const transcript = page.getByRole('region', { name: 'Hội thoại đặt xe' })
  await expect(transcript.locator('.bubble')).toHaveText([response.text])
  await expect(transcript.getByRole('button')).toHaveCount(0)
  await expect(page.locator('.choices, .trip-card, .booking-card, .inquiry-card, .suggestions')).toHaveCount(0)
  await expect(page.getByText('Dữ liệu lựa chọn chỉ dành cho server')).toHaveCount(0)
  await expect(page.getByText('Dữ liệu tóm tắt chỉ dành cho server')).toHaveCount(0)
  await expect(page.getByText('Gemini tối đa 15 yêu cầu/phút')).toHaveCount(0)
  await page.getByRole('textbox', { name: 'Tin nhắn đặt xe' }).fill('1')
  await page.getByRole('button', { name: 'Gửi tin nhắn' }).click()
  await expect(transcript.locator('.message.user .bubble')).toHaveText('1')
  await expect(transcript.locator('.message.bot .bubble').last()).toHaveText('Mình đã nhận lựa chọn bằng văn bản của bạn.')
  expect(chat.messages[0]).toMatchObject({ text: '1', reply_to_response_id: response.response_id })
  expect(chat.messages[0].rendered_response_ids).toContain(response.response_id)
  expect(chat.actions).toEqual([])
})

test('retry keeps the original text, message ID and reply context when the draft changes', async ({ page }) => {
  const chat = await mockTextChat(page, true)
  await page.goto('/')
  const input = page.getByRole('textbox', { name: 'Tin nhắn đặt xe' })
  await expect(page.locator('.message.bot')).toHaveCount(1)
  await input.fill('2')
  await page.getByRole('button', { name: 'Gửi tin nhắn' }).click()
  await expect(page.getByRole('alert')).toContainText('Mất kết nối tạm thời')
  await input.fill('Nội dung tiếp theo chưa gửi')
  await page.getByRole('button', { name: 'Gửi lại', exact: true }).click()
  await expect(page.locator('.message.user .bubble')).toHaveText('2')
  await expect(input).toHaveValue('Nội dung tiếp theo chưa gửi')
  expect(chat.messages).toHaveLength(2)
  expect(chat.messages[1]).toEqual(chat.messages[0])
  expect(chat.actions).toEqual([])
  await expect(page.getByRole('region', { name: 'Hội thoại đặt xe' }).getByRole('button')).toHaveCount(0)
})
