# Đặt xe bằng giọng nói

Web nhận tên và số điện thoại, xin quyền micro rồi kết nối LiveKit Cloud.
LiveKit Agents dùng Azure Speech STT (`vi-VN`), gọi lõi hội thoại hiện có qua
FastAPI, rồi đọc câu trả lời bằng Azure Speech TTS. Groq/Gemini vẫn diễn giải
nhu cầu đặt xe. Agent không mở SQLite và không tự tạo booking.

## Cấu hình

Thêm các biến sau vào `src/backend/.env` hiện có; giữ nguyên các key Groq,
Gemini và VietMap. Không đưa secret vào biến Vite hoặc frontend.

```dotenv
VOICE_ENABLED=true
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-key
LIVEKIT_API_SECRET=your-livekit-secret
LIVEKIT_AGENT_NAME=parrotgo-booking
VOICE_AGENT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
VOICE_API_URL=http://127.0.0.1:8000
AZURE_SPEECH_KEY=your-azure-speech-key
AZURE_SPEECH_REGION=southeastasia
AZURE_SPEECH_VOICE=vi-VN-HoaiMyNeural
```

Sinh secret bằng `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Script Windows tự thêm các thiết lập bridge còn thiếu và sinh secret cục bộ,
không thay đổi các key provider hiện có.
Nếu agent chạy máy khác hoặc trên LiveKit Cloud, `VOICE_API_URL` phải là URL
HTTPS của backend mà worker truy cập được. API và worker dùng cùng
`VOICE_AGENT_SECRET`, cùng dự án LiveKit và cùng `LIVEKIT_AGENT_NAME`.
Chỉ worker cần các key Azure; chỉ backend cần key VietMap và LLM.
TTS dùng endpoint synthesis theo region; `AZURE_TTS_ENDPOINT` có thể ghi đè
nếu dùng endpoint riêng. Biến `AZURE_SPEECH_ENDPOINT` chứa URL gốc Cognitive
Services không được dùng trực tiếp làm endpoint TTS.

## Chạy Windows

1. Chạy `run-windows.bat` để cài/build và mở API/web.
2. Mở terminal thứ hai, chạy `.\run-voice-agent.bat`.
3. Mở `http://127.0.0.1:8000`, nhập tên/SĐT, bấm bắt đầu và cho phép micro.
   Nếu trình duyệt chặn âm thanh, bấm **Bật âm thanh**.

Worker đăng ký với LiveKit Cloud; không cần mở cổng inbound cho worker cục bộ.
Web triển khai công khai cần HTTPS để trình duyệt cho phép micro.
Node 22.22+ là yêu cầu của dependency LiveKit client hiện hành.
`VOICE_ENABLED=false` dùng lại giao diện chat để phát triển/test offline.

Linux/container: cài `uv sync --project src/backend --extra voice --locked`,
chạy `python -m livekit.agents download-files` rồi
`python -m app.voice.agent start` từ thư mục backend. Worker cần thư viện native
cho Azure Speech/LiveKit trên OS tương ứng; Dockerfile API hiện tại chỉ phục vụ
API/web. Không chạy thêm coordinator hay nhiều API workers chung database.

## Chọn địa điểm và xác nhận

- Chỉ đọc tối đa hai kết quả đầu tiên theo thứ tự VietMap, không lấy kết quả
  thứ ba để thay thế. Không mở rộng một POI thành nhiều cổng trong danh sách voice.
- Nhận “thứ nhất”, “thứ hai”, số thứ tự hoặc tên địa điểm. Chuẩn hóa dấu
  tiếng Việt và so độ tương đồng; tên chung/tên quá giống nhau cần hỏi lại.
  Nhu cầu phức hợp, đổi địa chỉ, phủ định được gửi tới extractor hiện có.
- Khách có thể nói lại địa chỉ nếu cả hai chưa đúng. Chỉ có một kết quả thì
  đọc kết quả đó để xác nhận; lỗi VietMap không tự chọn điểm khác hay bịa tọa độ.
- Transcript trên màn hình không tạo ACK trong chế độ voice. Worker gửi ACK
  sau khi LiveKit phát xong lời bot; phản hồi bị gián đoạn/lỗi âm thanh không ACK.
  Bot đọc xong rồi mới nghe lượt kế tiếp trong phiên bản này.
- Tạo/hủy đơn vẫn cần xác nhận tóm tắt hiện hành. Kết nối lại vô hiệu hóa
  worker của phòng cũ, giữ phiên/chuyến và đọc lại phản hồi hiện tại.

Đơn vẫn là sandbox `SBX-`, chưa điều phối tài xế thật. Cần kiểm thử audio end-to-end
với tài khoản LiveKit/Azure của bạn trước khi triển khai cho khách.

Smoke test cloud từ gốc dự án:
`src/backend/.venv/Scripts/python.exe src/backend/examples/smoke_voice_live.py --live`.
Lệnh dùng key LiveKit/Azure đã cấu hình, mở API fixture riêng ở cổng 8002,
nhận âm thanh lời chào, phát câu hỏi loại xe bằng audio tổng hợp, kiểm tra STT
và phản hồi lõi hội thoại, rồi dừng tiến trình thử. Không tạo booking; không
thay đổi database vận hành. Đây là kiểm tra kết nối, chưa đánh giá nhận dạng
giọng nói người thật trong môi trường có tiếng ồn.

Tài liệu SDK: [Azure STT](https://docs.livekit.io/agents/models/stt/azure/),
[Azure TTS](https://docs.livekit.io/agents/models/tts/azure/),
[agent dispatch](https://docs.livekit.io/agents/server/agent-dispatch/).

## Docker và VPS

Image đã bao gồm LiveKit/Azure; xem [triển khai và thay API key trên VPS](deploy/VOICE_VPS.md). GitHub Actions chạy kiểm thử, build image, publish GHCR và xuất artifact. Không cần đưa API key vào GitHub.
