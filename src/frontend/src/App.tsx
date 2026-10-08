import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, type Bootstrap, type Event, type MessageInput, type Snapshot } from './api'
import VoiceCall from './VoiceCall'

const uid = () => crypto.randomUUID()
export default function App() {
  const [config, setConfig] = useState<Bootstrap>()
  const [snapshot, setSnapshot] = useState<Snapshot>()
  const [events, setEvents] = useState<Event[]>([])
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [customerName, setCustomerName] = useState('')
  const [customerPhone, setCustomerPhone] = useState('')
  const [retry, setRetry] = useState<{ path: string; body: unknown; text?: string }>()
  const session = useRef('')
  const cursor = useRef(0)
  const acked = useRef(new Set<string>())
  const rendered = useRef(new Set<string>())
  const conversation = useRef<HTMLElement>(null)
  const conversationContent = useRef<HTMLDivElement>(null)
  const followingLatest = useRef(true)
  const booting = useRef(false)

  const merge = useCallback((next: Snapshot) => {
    setSnapshot(next)
    setEvents(old => {
      const ids = new Set(old.map(e => e.event_id))
      const added = next.events.filter(e => !ids.has(e.event_id))
      return added.length ? [...old, ...added].sort((a, b) => a.cursor - b.cursor) : old
    })
    cursor.current = Math.max(cursor.current, next.next_cursor)
  }, [])

  const start = useCallback(async (fresh = false, customer?: { customer_name: string; customer_phone: string }) => {
    setError('')
    const bootstrap = await api<Bootstrap>('/bootstrap')
    setConfig(bootstrap)
    const saved = fresh ? null : localStorage.getItem('di-cung-session')
    if (saved) {
      try {
        const next = await api<Snapshot>(`/sessions/${saved}`)
        if (next.architecture_version === 'architecture-fixed-1') {
          session.current = saved
          setCustomerName(next.customer_name || '')
          setCustomerPhone(next.customer_phone || '')
          merge(next)
          return
        }
        localStorage.removeItem('di-cung-session')
        localStorage.removeItem('di-cung-session-key')
      } catch { localStorage.removeItem('di-cung-session') }
    }
    if (!customer) return
    let key = fresh ? uid() : localStorage.getItem('di-cung-session-key') || uid()
    localStorage.setItem('di-cung-session-key', key)
    const next = await api<Snapshot>('/sessions', { client_session_key: key, ...customer })
    session.current = next.session_id
    localStorage.setItem('di-cung-session', next.session_id)
    merge(next)
  }, [merge])

  useEffect(() => {
    if (booting.current) return
    booting.current = true
    start().catch(e => setError(e.message))
  }, [start])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (!stopped && session.current && document.visibilityState === 'visible') {
        try {
          let next: Snapshot
          do {
            next = await api<Snapshot>(`/sessions/${session.current}/updates?after_cursor=${cursor.current}`)
            if (stopped) return
            merge(next)
          } while (next.has_more)
        } catch (e) { if (!stopped) setError((e as Error).message) }
      }
      if (!stopped) timer = setTimeout(poll, 1000)
    }
    timer = setTimeout(poll, 500)
    const resume = () => { if (document.visibilityState === 'visible') { clearTimeout(timer); void poll() } }
    document.addEventListener('visibilitychange', resume)
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener('visibilitychange', resume) }
  }, [merge])

  const scrollToLatest = useCallback(() => {
    const element = conversation.current
    // Scroll only this panel. Instant scrolling cannot keep pulling against the user.
    if (followingLatest.current && element) element.scrollTop = element.scrollHeight
  }, [])

  useLayoutEffect(scrollToLatest, [
    events, snapshot?.pending_count, snapshot?.waiting_for_quota,
    snapshot?.booking?.booking_id, snapshot?.booking?.status, snapshot?.booking?.provider_status,
    scrollToLatest,
  ])

  useEffect(() => {
    // Account for preview resizing, wrapped text and fonts loading after render.
    const observer = new ResizeObserver(scrollToLatest)
    if (conversation.current) observer.observe(conversation.current)
    if (conversationContent.current) observer.observe(conversationContent.current)
    return () => observer.disconnect()
  }, [scrollToLatest])

  useEffect(() => {
    // Polls still retry failed delivery acknowledgements even without new events.
    if (config?.capabilities.voice_booking) return
    for (const event of events) {
      if (event.type !== 'assistant_response') continue
      const response = event.payload
      if (!response) continue
      rendered.current.add(response.response_id)
      if (acked.current.has(response.response_id) || !session.current) continue
      acked.current.add(response.response_id)
      api(`/sessions/${session.current}/delivery-acks`, {
        ack_id: 'ack-' + response.response_id, response_id: response.response_id,
        generation: response.generation, delivery_type: 'rendered',
      }).catch(() => acked.current.delete(response.response_id))
    }
  }, [events, snapshot, config])

  async function transmit(path: string, body: unknown, text?: string) {
    setSending(true); setError('')
    try {
      await api(path, body)
      setRetry(undefined)
      if (text) setInput(old => old === text ? '' : old)
      const next = await api<Snapshot>(`/sessions/${session.current}/updates?after_cursor=${cursor.current}`)
      merge(next)
    } catch (e) {
      setError((e as Error).message); setRetry({ path, body, text })
    } finally { setSending(false) }
  }

  function send() {
    const text = input.trim()
    if (!text || sending || !session.current || snapshot?.needs_support) return
    void transmit(`/sessions/${session.current}/messages`, {
      client_message_id: uid(), text, reply_to_response_id: snapshot?.active_response?.response_id || null,
      rendered_response_ids: Array.from(rendered.current).slice(-20),
    } satisfies MessageInput, input)
  }

  async function fresh() {
    if (sending || snapshot?.pending_count) return
    session.current = ''; cursor.current = 0
    localStorage.removeItem('di-cung-session')
    localStorage.removeItem('di-cung-session-key')
    followingLatest.current = true
    acked.current.clear(); rendered.current.clear(); setEvents([]); setSnapshot(undefined)
    setRetry(undefined)
    await start(true).catch(e => setError(e.message))
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/"><span className="brand-mark">P</span><span>{config?.brand_name || 'ParrotGo'}<small>CHUYẾN ĐI BẮT ĐẦU TỪ MỘT CÂU</small></span></a>
      <div className="intro"><span className="eyebrow">TRỢ LÝ ĐẶT XE</span><h1>Bạn muốn<br />đi đâu hôm nay?</h1><p>Nói điểm đón, điểm đến và nhu cầu của bạn. Mình sẽ giúp bạn hoàn thiện chuyến đi.</p></div>
      <div className="steps"><div><b>01</b><span>Kể về chuyến đi</span></div><div><b>02</b><span>Kiểm tra địa điểm & giá</span></div><div><b>03</b><span>Xác nhận khi sẵn sàng</span></div></div>
      <div className="sandbox-note"><span>CHẾ ĐỘ THỬ NGHIỆM</span><p>Bot báo giá/km. Tiền cuối cùng tính theo đồng hồ và quãng đường thực tế. Chưa điều phối tài xế thật.</p></div>
      <div className="sidebar-foot">Một cuộc trò chuyện. Một chuyến đi rõ ràng.</div>
    </aside>
    <main className="chat-panel">
      <header className="chat-header"><div><span className="assistant-avatar">P</span><div><strong>Trợ lý {config?.brand_name || 'ParrotGo'}</strong><small><i />{config?.llm_provider === 'fixture' ? 'Demo offline' : 'Sẵn sàng hỗ trợ'}</small></div></div><button className="new-trip" onClick={() => void fresh()} disabled={sending || !!snapshot?.pending_count}>+ Chuyến mới</button></header>
      <div className="mode-bar"><span>ĐẶT XE THỬ NGHIỆM</span><p>{config?.maps_provider === 'vietmap' ? 'Địa điểm qua VietMap' : 'Địa điểm mẫu Hà Nội & TP.HCM'} · {config?.capabilities.voice_booking ? 'Trò chuyện bằng giọng nói' : 'Trả lời bằng tin nhắn'}</p></div>
      <section className="conversation" ref={conversation} aria-label="Hội thoại đặt xe" aria-live="polite" onScroll={event => {
        const element = event.currentTarget
        followingLatest.current = element.scrollHeight - element.clientHeight - element.scrollTop <= 48
      }}>
        <div className="conversation-content" ref={conversationContent}>
          {!snapshot && <form className="session-profile" onSubmit={event => {
            event.preventDefault()
            setSending(true)
            void start(false, { customer_name: customerName.trim(), customer_phone: customerPhone.trim() })
              .catch(e => setError(e.message)).finally(() => setSending(false))
          }}>
            <h2>{config?.capabilities.voice_booking ? 'Nhập tên và số điện thoại để nói chuyện' : 'Bắt đầu phiên đặt xe'}</h2>
            <label>Tên khách hàng<input aria-label="Tên khách hàng" autoComplete="name" required maxLength={128} value={customerName} onChange={event => setCustomerName(event.target.value)} /></label>
            <label>Số điện thoại<input aria-label="Số điện thoại" type="tel" autoComplete="tel" required value={customerPhone} onChange={event => setCustomerPhone(event.target.value)} /></label>
            <button disabled={sending || !customerName.trim() || !customerPhone.trim()}>{config?.capabilities.voice_booking ? 'Bắt đầu nói chuyện' : 'Bắt đầu'}</button>
          </form>}
          <div className="day-label">HÔM NAY</div>
          {events.map(event => {
            if (event.type === 'booking_updated') return null
            if (event.type === 'turn_failed') return <div className="turn-error" key={event.event_id}>{event.payload.message}</div>
            const response = event.type === 'assistant_response' ? event.payload : null
            const user = event.type === 'message_received'
            if (!user && !response) return null
            return <article className={'message ' + (user ? 'user' : 'bot')} key={event.event_id}>
              {!user && <span className="message-avatar">P</span>}
              <div className="message-content"><div className="bubble">{event.type === 'message_received' ? event.payload.text : response?.text}</div>
                <time>{new Date(event.occurred_at).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })}</time>
              </div>
            </article>
          })}
        </div>
      </section>
      <footer className="composer-area">
        {!!snapshot?.pending_count && <div className="working" role="status"><span /><span /><span /><p>{snapshot.waiting_for_quota ? 'Yêu cầu đã lưu, đang chờ đến lượt xử lý. Bạn không cần gửi lại.' : 'Đang xử lý yêu cầu của bạn…'}</p></div>}
        {snapshot?.needs_support && <div className="error" role="alert">Phiên cần được kiểm tra. Hệ thống đã dừng thử lại tự động; kết quả giao dịch chưa rõ vẫn được lưu để đối soát.</div>}
        {error && <div className="error" role="alert">{error}{retry ? <button disabled={sending} onClick={() => void transmit(retry.path, retry.body, retry.text)}>Gửi lại</button> : <button onClick={() => void start().catch(e => setError(e.message))}>Kết nối lại</button>}</div>}
        {config?.capabilities.voice_booking ? (snapshot && !snapshot.needs_support && <VoiceCall key={snapshot.session_id} sessionId={snapshot.session_id} />) : <>
          <div className="composer"><textarea aria-label="Tin nhắn đặt xe" placeholder="Ví dụ: Đón tôi ở Nhà hát Lớn, đến Ga Hà Nội…" value={input} maxLength={2000} rows={2} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }} /><button aria-label="Gửi tin nhắn" disabled={sending || !input.trim() || !snapshot || snapshot.needs_support} onClick={send}>↑</button></div>
          <div className="composer-hint"><span>Enter để gửi · Shift + Enter để xuống dòng</span><span>{input.length}/2000</span></div>
        </>}
      </footer>
    </main>
  </div>
}
