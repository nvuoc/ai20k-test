import { expect, test, type Locator, type Page } from '@playwright/test'
import type { Bootstrap, Event, Snapshot } from '../src/api'

function message(cursor: number): Event {
  return {
    cursor, event_id: `event-${cursor}`, occurred_at: '2026-10-03T08:00:00+07:00',
    type: 'assistant_response',
    payload: {
      response_id: `response-${cursor}`, generation: cursor,
      text: `Tin nhắn ${cursor}\nNội dung hội thoại đặt xe để kiểm tra việc đọc lại lịch sử.\nĐiểm đón và điểm đến của chuyến đi được giữ trong hội thoại.`,
      action: 'ask', focus: null, candidates: [], summary: null,
      presentation: { contract_version: 'chat-presentation-2', response_id: `response-${cursor}`, generation: cursor },
      booking_status: 'collecting_info', reason: null, booking: null,
    },
  }
}

async function mockChat(page: Page, failFirstAck = false) {
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  const bootstrap: Bootstrap = {
    api_version: 'chat-api-2', mode: 'sandbox', profile: 'test', llm_provider: 'fixture',
    model: null, maps_provider: 'fixture', booking_provider: 'sandbox', gemini_rpm: 15,
    capabilities: { asap: true, scheduled: false, multi_stop: false },
  }
  const history = Array.from({ length: 24 }, (_, index) => message(index + 1))
  const state: Snapshot = {
    api_version: 'chat-api-2', session_id: 'scroll-session', events: history,
    next_cursor: history.length, current_cursor: history.length, has_more: false,
    pending_count: 0, booking_status: 'collecting_info', draft_id: 'scroll-draft',
    active_response: null, booking: null,
  }
  let polls = 0
  let sessions = 0
  let latestAckAttempts = 0
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    let body: unknown
    if (url.pathname === '/api/bootstrap') body = bootstrap
    else if (url.pathname.endsWith('/delivery-acks')) {
      if (route.request().postDataJSON().response_id === 'response-24') {
        latestAckAttempts++
        if (failFirstAck && latestAckAttempts === 1) {
          await route.fulfill({ status: 503, json: { message: 'Temporary connection error' } })
          return
        }
      }
      body = { status: 'acknowledged' }
    }
    else if (url.pathname === '/api/sessions') {
      if (sessions++ > 0) {
        history.splice(0, history.length, message(1))
        state.next_cursor = state.current_cursor = 1
      }
      body = state
    } else if (url.pathname.endsWith('/updates')) {
      const after = Number(url.searchParams.get('after_cursor'))
      body = { ...state, events: history.filter(event => event.cursor > after) }
      polls++
    } else body = state
    await route.fulfill({ json: body })
  })
  return {
    state,
    get polls() { return polls },
    get latestAckAttempts() { return latestAckAttempts },
    append() {
      const next = message(history.length + 1)
      history.push(next)
      state.next_cursor = state.current_cursor = next.cursor
    },
  }
}

const distanceFromBottom = (conversation: Locator) => conversation.evaluate(element =>
  element.scrollHeight - element.clientHeight - element.scrollTop)

async function nextPoll(page: Page) {
  await page.waitForResponse(response => response.url().includes('/updates?after_cursor='))
  // Let React commit and the browser dispatch scroll/resize events.
  await page.evaluate(() => new Promise<void>(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
}

for (const viewport of [
  { name: 'desktop', width: 1280, height: 720 },
  { name: 'compact VS Code tab', width: 820, height: 560 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`history stays readable during polling and new replies (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    const chat = await mockChat(page)
    await page.goto('/')
    const conversation = page.getByRole('region', { name: 'Hội thoại đặt xe' })
    await expect(conversation.locator('.message')).toHaveCount(24)
    await expect(conversation.locator('.working')).toHaveCount(0)
    await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(30)
    expect(await conversation.evaluate(element => element.scrollHeight - element.clientHeight)).toBeGreaterThan(500)

    await conversation.hover()
    await page.mouse.wheel(0, -600)
    await expect.poll(() => distanceFromBottom(conversation)).toBeGreaterThan(400)
    const position = await conversation.evaluate(element => element.scrollTop)
    for (let i = 0; i < 3; i++) {
      await nextPoll(page)
      expect(Math.abs(await conversation.evaluate(element => element.scrollTop) - position)).toBeLessThan(2)
    }
    // History must scroll inside the panel while the composer remains in view.
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(viewport.height)
    await expect(page.getByRole('textbox', { name: 'Tin nhắn đặt xe' })).toBeInViewport()

    chat.append()
    await expect(conversation.locator('.message')).toHaveCount(25)
    expect(Math.abs(await conversation.evaluate(element => element.scrollTop) - position)).toBeLessThan(2)
    chat.state.pending_count = 1
    await expect(page.locator('.working')).toBeVisible()
    await expect(conversation.locator('.working')).toHaveCount(0)
    expect(Math.abs(await conversation.evaluate(element => element.scrollTop) - position)).toBeLessThan(2)
    chat.state.waiting_for_quota = true
    await expect(page.locator('.working')).toContainText('chờ đến lượt xử lý')
    expect(Math.abs(await conversation.evaluate(element => element.scrollTop) - position)).toBeLessThan(2)
    chat.state.pending_count = 0
    await expect(page.locator('.working')).toHaveCount(0)

    await conversation.hover()
    await page.mouse.wheel(0, 10000)
    await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
    chat.append()
    await expect(conversation.locator('.message')).toHaveCount(26)
    await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
    chat.state.pending_count = 1
    await expect(page.locator('.working')).toBeVisible()
    await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
    chat.state.pending_count = 0
    await expect(page.locator('.working')).toHaveCount(0)

    await conversation.hover()
    await page.mouse.wheel(0, -600)
    await expect.poll(() => distanceFromBottom(conversation)).toBeGreaterThan(400)
    await page.getByRole('button', { name: '+ Chuyến mới' }).click()
    await expect(conversation.locator('.message')).toHaveCount(1)
    for (let i = 0; i < 24; i++) chat.append()
    await expect(conversation.locator('.message')).toHaveCount(25)
    await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
    expect(chat.polls).toBeGreaterThan(3)
  })
}

test('chat in an iframe scrolls independently and adapts when the preview is resized', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  const chat = await mockChat(page)
  await page.route('**/preview-host', route => route.fulfill({
    contentType: 'text/html',
    body: '<body style="margin:0;height:2400px;padding-top:220px"><iframe title="VS Code preview" src="/" style="width:820px;height:440px;border:0"></iframe></body>',
  }))
  await page.goto('/preview-host')
  const preview = page.getByTitle('VS Code preview')
  const conversation = page.frameLocator('iframe').getByRole('region', { name: 'Hội thoại đặt xe' })
  await expect(conversation.locator('.message')).toHaveCount(24)
  await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
  await page.evaluate(() => window.scrollTo(0, 120))
  chat.append()
  await expect(conversation.locator('.message')).toHaveCount(25)
  await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
  expect(await page.evaluate(() => window.scrollY)).toBe(120)

  await preview.evaluate(element => { element.style.height = '360px' })
  await expect.poll(() => distanceFromBottom(conversation)).toBeLessThan(2)
  await conversation.hover()
  await page.mouse.wheel(0, -600)
  await expect.poll(() => distanceFromBottom(conversation)).toBeGreaterThan(400)
  const position = await conversation.evaluate(element => element.scrollTop)
  await preview.evaluate(element => { element.style.height = '400px' })
  chat.append()
  await expect(conversation.locator('.message')).toHaveCount(26)
  expect(Math.abs(await conversation.evaluate(element => element.scrollTop) - position)).toBeLessThan(2)
  expect(await page.evaluate(() => window.scrollY)).toBe(120)
})

test('delivery acknowledgements retry after a connection error without new messages', async ({ page }) => {
  const chat = await mockChat(page, true)
  await page.goto('/')
  await expect(page.locator('.message')).toHaveCount(24)
  await expect.poll(() => chat.latestAckAttempts).toBeGreaterThanOrEqual(2)
  await expect(page.locator('.message')).toHaveCount(24)
})
