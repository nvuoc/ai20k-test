import { expect, test, type Page } from '@playwright/test'
import type { Snapshot, Summary } from '../src/api'

const full = 'Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567.'
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await page.getByRole('textbox', { name: 'Tên khách hàng' }).fill('An')
  await page.getByRole('textbox', { name: 'Số điện thoại' }).fill('0901234567')
  await page.getByRole('button', { name: 'Bắt đầu', exact: true }).click()
  await expect(page.locator('.message.bot').first()).toBeVisible()
})

const latestBot = (page: Page) => page.locator('.message.bot .bubble').last()

async function snapshot(page: Page): Promise<Snapshot> {
  return page.evaluate(async () => {
    const session = localStorage.getItem('di-cung-session')
    return (await fetch(`/api/sessions/${session}`)).json()
  })
}

async function sendText(page: Page, text: string) {
  await expect(page.locator('.message.bot').first()).toBeVisible()
  const count = await page.locator('.message.bot').count()
  await page.getByRole('textbox', { name: 'Tin nhắn đặt xe' }).fill(text)
  await page.getByRole('button', { name: 'Gửi tin nhắn' }).click()
  await expect.poll(() => page.locator('.message.bot').count()).toBeGreaterThan(count)
  await expect(page.locator('.working')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Hội thoại đặt xe' }).getByRole('button')).toHaveCount(0)
  return snapshot(page)
}

async function confirmLocations(page: Page) {
  let current = await snapshot(page)
  for (let attempts = 0; current.active_response?.action === 'confirm_slots'; attempts++) {
    expect(attempts, 'Location consent must finish without repeating a resolved proposal').toBeLessThan(10)
    expect(current.booking).toBeNull()
    current = await sendText(page, 'Đúng')
  }
  return current
}

async function prepareBooking(page: Page) {
  await sendText(page, full)
  const current = await confirmLocations(page)
  expect(current.active_response?.action).toBe('confirm_booking')
  await expect(latestBot(page)).toContainText('đồng/km')
  return current
}

test('text conversation books, survives reload and cancels without action controls', async ({ page }) => {
  const errors: string[] = []
  const actionRequests: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.url().endsWith('/actions')) actionRequests.push(request.url())
  })
  await page.goto('/')
  await expect(page.getByText('Demo offline')).toBeVisible()
  const prepared = await prepareBooking(page)
  expect((prepared.active_response?.summary as Summary)?.tariff.per_km).toBe(11500)
  await expect(latestBot(page)).toContainText(/11[.,]500/)
  const booked = await sendText(page, 'Đồng ý đặt xe')
  expect(booked.booking_status).toBe('booked')
  const bookingId = booked.booking?.booking_id
  expect(bookingId).toMatch(/^SBX-/)
  await expect(latestBot(page)).toContainText('Đã tạo cuốc xe thử nghiệm')
  await expect(latestBot(page)).toContainText(bookingId!)
  await page.reload()
  await expect(latestBot(page)).toContainText(bookingId!)
  const requested = await sendText(page, 'Hủy đơn thử nghiệm')
  expect(requested.booking_status).toBe('cancel_pending')
  await expect(latestBot(page)).toContainText('xác nhận hủy')
  const cancelled = await sendText(page, 'Đồng ý')
  expect(cancelled.booking_status).toBe('canceled')
  await expect(latestBot(page)).toContainText('Yêu cầu đặt xe đã hủy')
  expect(actionRequests).toEqual([])
  expect(errors).toEqual([])
})

test('text confirmation with a correction refreshes summary and does not book', async ({ page }) => {
  await page.goto('/')
  await prepareBooking(page)
  const oldSummary = await latestBot(page).innerText()
  const changed = await sendText(page, 'Đồng ý nhưng đổi sang xe 7 chỗ nhé')
  expect(changed.booking).toBeNull()
  expect(changed.active_response?.action).toBe('confirm_slots')
  await expect(latestBot(page)).toContainText('Ô tô 7 chỗ')
  expect(oldSummary).toContain('Ô tô 4 chỗ')
  await expect(page.getByText(oldSummary, { exact: true })).toBeAttached()
  await expect(page.locator('.message.bot').filter({ hasText: 'Đã tạo cuốc xe thử nghiệm' })).toHaveCount(0)
})

test('ambiguous destination presents numbered text and accepts a typed number', async ({ page }) => {
  await page.goto('/')
  await sendText(page, 'Đón tôi ở Nhà hát Lớn Hà Nội, đến Trường Sao Mai, đi ngay, 2 người, xe 4 chỗ, số 0901234567.')
  const pending = await confirmLocations(page)
  expect(pending.active_response?.candidates).toHaveLength(2)
  await expect(latestBot(page)).toContainText('1.')
  await expect(latestBot(page)).toContainText('2.')
  await expect(page.locator('.choices, .trip-card, .booking-card, .inquiry-card')).toHaveCount(0)
  await sendText(page, '1')
  const selected = await confirmLocations(page)
  expect(selected.active_response?.candidates).toHaveLength(0)
  await expect(latestBot(page)).toContainText(pending.active_response!.candidates[0].label)
  await expect(latestBot(page)).not.toContainText('Lựa chọn hoặc phản hồi này đã cũ')
})

test('mobile composer fits and submits with Enter', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  const input = page.getByRole('textbox', { name: 'Tin nhắn đặt xe' })
  await expect(input).toBeVisible()
  const width = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(width).toBeLessThanOrEqual(390)
  await expect(page.locator('.message.bot').first()).toBeVisible()
  await input.fill('Tôi muốn đi Ga Hà Nội')
  await input.press('Enter')
  await expect(page.locator('.message.user')).toHaveCount(1)
  await expect(page.getByRole('region', { name: 'Hội thoại đặt xe' }).getByRole('button')).toHaveCount(0)
})

test('route inquiry changes vehicle and promotes through text into a separate summary', async ({ page }) => {
  await page.goto('/')
  await prepareBooking(page)
  const bookingSummary = await latestBot(page).innerText()
  await sendText(page, 'Từ Nhà hát Lớn Hà Nội đến Bạch Mai cổng sau bao nhiêu km và giá bao nhiêu?')
  const inquiry = await confirmLocations(page)
  expect(inquiry.active_response?.inquiry?.can_use_route).toBe(true)
  expect(inquiry.booking).toBeNull()
  await expect(latestBot(page)).toContainText('Bạch Mai')
  await expect(page.getByText(bookingSummary, { exact: true })).toBeAttached()
  await page.reload()
  await expect(latestBot(page)).toContainText('Bạch Mai')
  const vehicle = await sendText(page, 'Tuyến vừa hỏi nếu đi xe 7 chỗ bao nhiêu km và giá bao nhiêu?')
  expect(vehicle.active_response?.inquiry?.vehicle).toBe('oto_7_cho')
  await expect(latestBot(page)).toContainText('Ô tô 7 chỗ')
  await expect(page.getByText(bookingSummary, { exact: true })).toBeAttached()
  await sendText(page, 'Dùng tuyến vừa hỏi này để đặt xe')
  const promoted = await confirmLocations(page)
  expect(promoted.active_response?.action).toBe('confirm_booking')
  expect(promoted.booking).toBeNull()
  await expect(latestBot(page)).toContainText('cổng sau')
  await expect(latestBot(page)).toContainText('Ô tô 7 chỗ')
})

test('weather answer in offline mode labels its sample source in text', async ({ page }) => {
  await page.goto('/')
  await sendText(page, 'Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?')
  await confirmLocations(page)
  await expect(latestBot(page)).toContainText('không phải dự báo thực tế')
  await expect(latestBot(page)).toContainText('28°C')
  expect((await snapshot(page)).booking).toBeNull()
})

test('Mega destination uses its configured default without asking for a gate', async ({ page }) => {
  await sendText(page, full.replace('Ga Hà Nội', 'Ocean Park 1'))
  const prepared = await confirmLocations(page)
  expect(prepared.active_response?.action).toBe('confirm_booking')
  const summary = prepared.active_response!.summary as Summary
  expect(summary.destination).toContain('Ocean Park 1')
  expect(summary.tariff.final_amount_basis).toBe('meter')
  await page.reload()
  expect((await snapshot(page)).active_response?.summary).toEqual(summary)
})

test('Mega pickup asks once then uses a default with a driver call note', async ({ page }) => {
  const requested = await sendText(page, full.replace('Nhà hát Lớn Hà Nội', 'Sân bay Nội Bài'))
  expect(requested.active_response?.action).toBe('clarify_address')
  await sendText(page, 'Không biết, đừng hỏi nữa')
  const prepared = await confirmLocations(page)
  expect(prepared.active_response?.action).toBe('confirm_booking')
  const summary = prepared.active_response!.summary as Summary
  expect(summary.pickup_note).toContain('gọi khách')
  expect(summary.pickup).toContain('T1')
  expect(prepared.booking).toBeNull()
})
