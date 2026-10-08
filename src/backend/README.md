# Backend theo architecture_fixed.md

HTTP, Text CLI và Python text adapter dùng chung `app/domain/booking_engine.py`. Kiến trúc hiện hành: [architecture_fixed.md](../../architecture_fixed.md); bảng đối chiếu: [ARCHITECTURE_IMPLEMENTATION.md](../../ARCHITECTURE_IMPLEMENTATION.md).

| Thành phần | Trách nhiệm |
| --- | --- |
| `contracts/booking.py` | Customer, BotState, AddressSlot, StopoverSlot, ValueSlot và readiness suy ra từ slot |
| `domain/booking_engine.py` | Reducer duy nhất cho booking_slots, multi-intent, xác nhận theo phạm vi, hủy, geocode, Mega POI và dispatch |
| `contracts/turn.py`, `prompts/turn_v2.txt` | Interpretation có evidence; projection architecture_state cho model |
| `adapters/knowledge_base.py` | Tra cứu FAQ và đơn giá/km có version; không tính tiền trọn chuyến |
| `adapters/crm_sqlite.py` | Lịch sử địa chỉ theo SĐT, gợi ý phải được xác nhận |
| `domain/mega_poi.py` | Alias và default pickup/drop-off được cấu hình; tọa độ lấy từ bản đồ |
| `domain/inquiries.py`, map/weather adapters | Hỏi tuyến, vị trí, quãng đường, thời gian và thời tiết với scope/freshness riêng |
| `adapters/extraction_router.py` | Groq chính, Gemini dự phòng; schema/evidence validation và quota bền vững |
| `graph/builder.py` | Checkpoint interpretation → prepare → finalize, phục hồi và dedup |
| `api_store.py`, `workers/coordinator.py` | Inbox, transcript/cursor, ACK, một writer cho mỗi phiên |
| `adapters/booking_sandbox.py` | Ledger tạo/hủy có idempotency; phục hồi commit mất phản hồi |
| `app/text.py`, `main.py` | Hàm main/main_async, TextBot và CLI |

`booking_slots` sở hữu dữ liệu nghiệp vụ. `booking_state`/`resolution` chỉ là projection cho NLU và read adapters. Provider receipt và giao dịch chưa rõ kết quả nằm trong `transaction`, tách khỏi trạng thái booking chuẩn.

## Khởi tạo phiên

Tên/SĐT là bắt buộc. Server sinh UUID cho từng hội thoại; không dùng SĐT hoặc client_session_key làm UUID. SĐT tra CRM. Hồ sơ khác không được dùng lại khóa của phiên đã tồn tại.

CLI từ thư mục gốc:

```powershell
src/backend/.venv/Scripts/python.exe src/backend/main.py --phone 0901234567 --name An
```

Không truyền --session sẽ mở hội thoại mới. Truyền lại một khóa --session để tiếp tục qua restart. Các ví dụ offline ở [examples/converse_text.py](examples/converse_text.py).

Python:

```python
from app.text import main

reply = main("Đón ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, xe 4 chỗ",
             session_id="conversation-key", customer_phone="0901234567", customer_name="An")
reply = main("đúng", session_id="conversation-key")
```

Đối số session_id của text adapter là khóa tiếp tục hội thoại; UUID nội bộ do server sinh. main_async/TextBot có cùng tham số. TextBot mặc định coi lời bot trả về đã được nhận khi có lượt tiếp theo; tích hợp TTS chưa phát xong truyền acknowledge_previous=False. Giữ message_id khi retry cùng đầu vào.

## HTTP

GET /api/bootstrap trước để nhận owner cookie, sau đó dùng cùng cookie.

| Method | Path | Kết quả |
| --- | --- | --- |
| POST | /api/sessions | client_session_key, customer_phone, customer_name; tạo/mở phiên idempotent |
| GET | /api/sessions/{id} | Snapshot, events, active_response, profile, architecture_version |
| POST | /api/sessions/{id}/messages | Lưu input và trả 202 receipt |
| POST | /api/sessions/{id}/actions | Chọn candidate, xác nhận tạo cuốc, yêu cầu/xác nhận hủy, thao tác inquiry |
| POST | /api/sessions/{id}/delivery-acks | Bằng chứng response hiện hành đã render |
| GET | /api/sessions/{id}/updates | Cursor/limit/has_more/next_cursor |
| GET | /api/sessions/{id}/booking | Kết quả provider công khai |
| GET | /api/sessions/{id}/weather | at có timezone, target pickup/destination, tùy chọn inquiry_id |
| GET | /api/health, /api/ready | Trạng thái nội bộ, không gọi provider |

Session body:

```json
{"client_session_key":"trip-one","customer_phone":"0901234567","customer_name":"An"}
```

Message body có client_message_id, text, reply_to_response_id và rendered_response_ids. ACK/reply phải thuộc phiên và presentation hiện hành; “đúng” chỉ xác nhận last_bot_action.target_slots. Sau xác nhận địa chỉ và slot, bot trình bày TripSummary với tariff.per_km, tariff.source, final_amount_basis=meter; không có fare/quote trọn chuyến.

Actions lấy refs từ response server: select_candidate dùng candidate_set_id/candidate_id; confirm_booking dùng prompt_id/booking_revision/snapshot_fingerprint. cancel_draft/cancel_booking chỉ **yêu cầu** hủy. Bot trả confirm_cancel; action confirm_cancel dùng prompt_id và ACK hiện hành mới thực hiện hủy. Giao diện web dùng lời khách dạng text cho các bước này.

Chọn/promote inquiry chỉ sửa draft và yêu cầu xác nhận mới, không cấp quyền tạo cuốc. Tra cứu giả định không sửa booking_slots. Readiness không phải consent. Giao dịch đang đối soát không được tạo/hủy thêm.

## Cấu hình

Xem [.env.example](.env.example). KNOWLEDGE_BASE_PATH là JSON versioned có policies và ba loại xe xe_may/oto_4_cho/oto_7_cho, đơn giá per_km và route_profile. MEGA_POI_PATH là JSON alias/default truy vấn; map adapter phải xác minh trước khi dùng. LOCAL_ALIAS_PATH/SERVICE_AREA_PATH cấu hình dữ liệu địa điểm. Lõi hiện hành luôn xác nhận các slot; flags tự chốt/phí hỗ trợ của V3 không bật hành vi cũ.

Router có tối đa hai provider calls với deadline chung; không tự chuyển sang fixture khi dịch vụ live lỗi. Quota/cooldown được lưu riêng. Lỗi cấu hình báo lỗi dịch vụ và giữ phiên để khách có thể gửi tiếp; rate limit giữ input chờ xử lý. Key, URL chứa key và state nội bộ không đi ra public projection.

Scheduled pickup giữ lời khách và thời điểm diễn giải có timezone/mốc tin nhắn, kiểm tra lại trước dispatch. Hành khách là tùy chọn; nếu có thì phải phù hợp loại xe. Stopovers được geocode, xác nhận và đưa vào từng chặng lộ trình.

LegacyConversationEngine/reducer/quote V1–V3 chỉ phục vụ hồi quy tương thích lịch sử. Phiên thiếu hồ sơ không tái sử dụng consent cũ; mở phiên mới có tên/SĐT, giữ dữ liệu/ledger cũ để kiểm tra.

Provider tạo/hủy vẫn là sandbox, chưa điều phối tài xế; operator_required là trạng thái chờ hỗ trợ. KB/địa điểm/tuyến đi kèm là dữ liệu thử nghiệm, cần cấu hình nguồn vận hành trước khi phục vụ khách thật.
