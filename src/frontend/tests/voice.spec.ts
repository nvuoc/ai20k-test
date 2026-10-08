import { expect, test } from '@playwright/test'

test.use({ launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] } })

test.beforeEach(async ({ page }) => {
  await page.route('**/api/bootstrap', async route => {
    const response = await route.fetch()
    const config = await response.json()
    config.capabilities.voice_booking = true
    config.capabilities.text_only_chat = false
    await route.fulfill({ response, json: config })
  })
})

test('name and phone start voice; missing configuration is actionable and transcripts do not ACK', async ({ page }) => {
  let acknowledgements = 0
  let voiceRequests = 0
  await page.route('**/delivery-acks', async route => {
    acknowledgements++
    await route.continue()
  })
  await page.route('**/api/sessions/*/voice', async route => {
    voiceRequests++
    await route.fulfill({ status: 503, json: { detail: 'Chưa cấu hình LiveKit và VOICE_AGENT_SECRET trên máy chủ.' } })
  })
  await page.goto('/')
  await page.getByLabel('Tên khách hàng').fill('Khách voice')
  await page.getByLabel('Số điện thoại').fill('0901234567')
  await page.getByRole('button', { name: 'Bắt đầu nói chuyện' }).click()
  await expect(page.getByText('Chưa cấu hình LiveKit và VOICE_AGENT_SECRET trên máy chủ.')).toBeVisible()
  await expect(page.getByLabel('Tin nhắn đặt xe')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Kết nối giọng nói' })).toBeVisible()
  expect(voiceRequests).toBe(1)
  expect(acknowledgements).toBe(0)
  await page.getByRole('button', { name: 'Kết nối giọng nói' }).click()
  await expect.poll(() => voiceRequests).toBe(2)
})

test('denied microphone shows recovery without creating a cloud room', async ({ page }) => {
  let voiceRequests = 0
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('Permission denied', 'NotAllowedError') }
  })
  await page.route('**/api/sessions/*/voice', async route => {
    voiceRequests++
    await route.abort()
  })
  await page.goto('/')
  await page.getByLabel('Tên khách hàng').fill('Khách micro')
  await page.getByLabel('Số điện thoại').fill('0901234567')
  await page.getByRole('button', { name: 'Bắt đầu nói chuyện' }).click()
  await expect(page.getByText('Hãy cho phép micro để nói chuyện với trợ lý.')).toBeVisible()
  expect(voiceRequests).toBe(0)
})
