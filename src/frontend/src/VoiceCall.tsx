import { useCallback, useEffect, useRef, useState } from 'react'
import { createLocalAudioTrack, type LocalAudioTrack, Room, RoomEvent, Track } from 'livekit-client'
import { api } from './api'

type Credentials = { server_url: string; participant_token: string; room: string }

export default function VoiceCall({ sessionId }: { sessionId: string }) {
  const room = useRef<Room | undefined>(undefined)
  const audio = useRef<HTMLDivElement>(null)
  const microphone = useRef<LocalAudioTrack | undefined>(undefined)
  const connecting = useRef(false)
  const alive = useRef(true)
  const [status, setStatus] = useState('Đang kết nối trợ lý…')
  const [connected, setConnected] = useState(false)
  const [muted, setMuted] = useState(false)
  const [error, setError] = useState('')
  const [audioBlocked, setAudioBlocked] = useState(false)

  const connect = useCallback(async () => {
    if (connecting.current) return
    connecting.current = true
    setError(''); setStatus('Đang kết nối trợ lý…')
    const current = new Room({ adaptiveStream: true, dynacast: true })
    try {
      await room.current?.disconnect()
      microphone.current?.stop()
      audio.current?.replaceChildren()
      room.current = current
      current.on(RoomEvent.TrackSubscribed, track => {
        if (track.kind === Track.Kind.Audio) audio.current?.appendChild(track.attach())
      })
      current.on(RoomEvent.TrackUnsubscribed, track => track.detach().forEach(element => element.remove()))
      current.on(RoomEvent.AudioPlaybackStatusChanged, () => setAudioBlocked(!current.canPlaybackAudio))
      current.on(RoomEvent.Disconnected, () => {
        if (alive.current && room.current === current) {
          setConnected(false); setStatus('Cuộc trò chuyện đã ngắt kết nối')
        }
        audio.current?.replaceChildren()
      })
      current.on(RoomEvent.Reconnecting, () => setStatus('Đang kết nối lại…'))
      current.on(RoomEvent.Reconnected, () => setStatus('Bạn có thể nói với trợ lý'))
      current.on(RoomEvent.ParticipantAttributesChanged, attributes => {
        const state = attributes['lk.agent.state']
        if (state) setStatus(({ listening: 'Đang nghe bạn…', thinking: 'Đang xử lý chuyến đi…', speaking: 'Trợ lý đang nói…', initializing: 'Trợ lý đang khởi động…' } as Record<string, string>)[state] || 'Bạn có thể nói với trợ lý')
      })
      const track = await createLocalAudioTrack({ echoCancellation: true, noiseSuppression: true })
      microphone.current = track
      if (!alive.current) { track.stop(); await current.disconnect(); return }
      const credentials = await api<Credentials>(`/sessions/${sessionId}/voice`, {})
      await current.connect(credentials.server_url, credentials.participant_token)
      if (!alive.current) { track.stop(); await current.disconnect(); return }
      await current.localParticipant.publishTrack(track, { source: Track.Source.Microphone })
      await current.startAudio().catch(() => setAudioBlocked(true))
      setConnected(true); setMuted(false); setStatus('Trợ lý đang khởi động…')
    } catch (e) {
      microphone.current?.stop()
      await current.disconnect()
      if (alive.current) {
        setConnected(false); setStatus('Chưa kết nối giọng nói')
        setError((e as Error).name === 'NotAllowedError' ? 'Hãy cho phép micro để nói chuyện với trợ lý.' : (e as Error).message)
      }
    } finally { connecting.current = false }
  }, [sessionId])

  useEffect(() => {
    alive.current = true
    void connect()
    return () => { alive.current = false; microphone.current?.stop(); void room.current?.disconnect() }
  }, [connect])

  async function toggleMicrophone() {
    try {
      await room.current?.localParticipant.setMicrophoneEnabled(muted)
      setMuted(!muted)
    } catch { setError('Không bật được micro. Hãy kiểm tra quyền truy cập micro.') }
  }

  return <div className="voice-call">
    <div className={'voice-orb' + (connected && !muted ? ' active' : '')} aria-hidden="true">🎙</div>
    <p role="status">{status}</p>
    <small>Nói điểm đón, điểm đến và loại xe. Trợ lý sẽ đọc lại để bạn xác nhận.</small>
    <div className="voice-controls">
      {connected ? <><button onClick={() => void toggleMicrophone()}>{muted ? 'Bật micro' : 'Tắt micro'}</button><button onClick={() => void room.current?.disconnect()}>Kết thúc trò chuyện</button></> : <button onClick={() => void connect()}>Kết nối giọng nói</button>}
      {audioBlocked && <button onClick={() => void room.current?.startAudio().catch(() => setError('Trình duyệt chưa cho phép phát âm thanh.'))}>Bật âm thanh</button>}
    </div>
    {error && <div className="error" role="alert">{error}</div>}
    <div ref={audio} hidden />
  </div>
}
