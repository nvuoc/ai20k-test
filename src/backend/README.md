# Backend chatbot đặt xe

FastAPI + LangGraph + SQLite, Groq GPT OSS 120B chính và Gemini Flash Lite dự phòng, VietMap và Open-Meteo. Lõi V3 dùng chung cho HTTP chat và `app.text.main(customer_text, session_id=...) -> str`. [Hướng dẫn cài/chạy và ví dụ main](../../README.md).

| Thành phần | Trách nhiệm |
| --- | --- |
| `app/main.py`, `config.py` | Lifespan, API, owner cookie, Origin, public projections, static UI, config. |
| `contracts/nlu.py`, `registry.py` | Strict input/output, đủ 12 slot, catalog/candidate validation. |
| `contracts/turn.py`, `prompts/turn_v2.txt` | Một interpretation cho booking acts, questions, inquiry actions và travel party; literal evidence. |
| `adapters/extractor.py` | Prompt, JSON parse, deadline/budget, repair tối đa 1 nếu caller bật. |
| `adapters/nlu_gemini.py`, `rate_limit.py` | generateContent, provider schema, quota 15 RPM/cooldown SQLite. |
| `adapters/nlu_groq.py`, `extraction_router.py`, `groq_rate_limit.py` | Strict chat completions, fallback có validation và deadline chung, quota RPM/token/cooldown riêng. |
| `adapters/nlu_fixture.py` | Demo offline có phạm vi rõ; không fallback khi Gemini lỗi. |
| `adapters/map_vietmap.py` | Search/place/route v4, cổng/ga, che key trong log. |
| `adapters/map_fixture.py`, `quote_fixture.py`, `fixtures/` | Điểm/tuyến và bảng giá sandbox có nguồn/version. |
| `domain/engine.py` | Apply cả lượt, issues, capacity/contact, revisions, consent, policy. |
| `domain/conversation.py`, `inquiries.py` | Migration, inquiry có scope riêng, tám nhóm câu hỏi, resume/promote và final dispatch guards. |
| `domain/location_parser.py`, `local_aliases.py` | Chuẩn hóa có giữ raw text, resolve anchor, alias có địa bàn/nguồn/version. |
| `domain/location_confirmation.py`, `location_policy.py`, `assistance_policy.py` | Location proposal có ACK/revision/TTL, area registry có nguồn, phí sandbox và consent riêng. |
| `contracts/weather.py`, `adapters/weather_open_meteo.py` | Forecast hourly có binding/coverage/TTL/đơn vị; lỗi nguồn không thành số 0. |
| `app/text.py`, `main.py` | `main` đồng bộ, `main_async`, `TextBot` chạy lâu và CLI. |
| `adapters/booking_sandbox.py` | Durable create/lookup/cancel, idempotency và fault test. |
| `graph/builder.py` | AsyncSqliteSaver, checkpoint/resume/dedup theo event. |
| `api_store.py`, `workers/coordinator.py` | Inbox, transcript/cursor, ACK, writer/phiên, phục hồi/đối soát. |

OpenAI adapter cũ được giữ cho tương thích/kiểm thử. Runtime mới dùng Groq với Gemini dự phòng; `LLM_PROVIDER=gemini` vẫn giữ chế độ Gemini-only.

## Contract và tài liệu

[MVP_PLAN.md](../../MVP_PLAN.md) là kế hoạch duy nhất; [bảng versions](../../MVP_PLAN.md#contracts) chốt contract hiện tại. [langgraph.md](../../langgraph.md), [llmextractor.md](../../llmextractor.md), [map.md](../../map.md) và [llmplanner.md](../../llmplanner.md) mô tả các boundary và giới hạn.

Trong tài liệu, ExtractorTurnResult là bí danh cho class TurnResult ở contracts/turn.py, không phải phản hồi bot. API trả AssistantResponse/ChatSnapshot; app.text.main trả str. MapAdapter trả dict đã chuẩn hóa theo MapResolution, status resolved và địa điểm tại place; không dùng unique/resolution/candidate_set làm wire schema hiện tại.

## HTTP API

Bootstrap trước để nhận cookie; dùng cùng cookie cho mọi request:

| Method | Path | Kết quả |
| --- | --- | --- |
| GET | `/api/bootstrap` | Owner cookie + profile/capabilities công khai. |
| POST | `/api/sessions` | `{client_session_key}` tạo/mở phiên idempotent. |
| GET | `/api/sessions/{id}` | Snapshot/events/active response/booking/pending_count. |
| POST | `/api/sessions/{id}/messages` | 202 sau khi lưu input, trả receipt. |
| POST | `/api/sessions/{id}/actions` | Typed select/confirm/cancel, cùng domain guards. |
| POST | `/api/sessions/{id}/delivery-acks` | Evidence đã render response hiện hành. |
| GET | `/api/sessions/{id}/updates` | after_cursor/limit/has_more/next_cursor. |
| GET | `/api/sessions/{id}/booking` | Đọc kết quả đã xuất bản. |
| GET | `/api/sessions/{id}/weather` | `at` có timezone, `target=pickup|destination`, tùy chọn `inquiry_id`; không sửa chuyến. |
| GET | `/api/health`, `/api/ready` | Không gọi provider, không trả secrets. |

Message body:

```json
{"client_message_id":"stable-on-retry","text":"Đón ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567","reply_to_response_id":null,"rendered_response_ids":[]}
```

Action body có `client_action_id`, `action`, `reply_to_response_id`, `rendered_response_ids`. Confirm action lấy server refs từ active_response.presentation/summary:

```json
{"type":"confirm_booking","prompt_id":"from-server","booking_revision":1,"snapshot_fingerprint":"from-server"}
```

Select action: `{type:"select_candidate",candidate_set_id,candidate_id}`. Cancel action: `{type:"cancel_booking",booking_id}` hoặc `{type:"cancel_draft",draft_id}`. Client không gửi GraphState, key, ngân sách, giá hoặc quyền provider.

API V2 bổ sung `{type:"use_inquiry_route",inquiry_id,inquiry_revision,booking_revision,route_fingerprint}`, `{type:"choose_inquiry_vehicle",inquiry_id,inquiry_revision,vehicle_type}`, `{type:"resume_booking"}` và `{type:"dismiss_inquiry",inquiry_id,inquiry_revision}`. Refs lấy từ active response; chọn tuyến chưa phải đồng ý đặt. Candidate/presentation có scope booking/inquiry. Bootstrap công bố brand, catalog và capability từ runtime. Snapshot có `waiting_for_quota`, `retry_at`; input đã lưu sẽ tiếp tục tự động.

## Gemini

Native HTTP dùng `x-goog-api-key`, model từ GEMINI_MODEL. Wire schema được rút về subset provider hỗ trợ; local Pydantic giữ toàn bộ constraints/cross-field/catalog/reference checks. JSON đúng schema không cấp quyền giao dịch.

Router dùng tối đa hai calls mỗi lần xử lý: một Groq, một Gemini nếu lỗi transport/output và còn deadline chung 25s. Không repair trong cùng provider khi router bật; không fallback fixture. Refusal/input sai dừng; lỗi key/model/schema chỉ degraded khi bật rõ `LLM_ALLOW_DEGRADED`. Mỗi provider có quota/cooldown bền vững riêng. Khi model đếm lệch offset, chỉ căn lại bằng literal duy nhất trong đầu vào; output/schema/evidence/catalog/version đều phải hợp lệ trước khi ghi state. Inbox retry lỗi dịch vụ có giới hạn, ACK/proposal và transaction idempotency vẫn được giữ.

V3 dùng location decisions qua văn bản, không thêm nút chat. Proposal bind nơi/phiên/revision/vehicle/policy/TTL và response đã render. “Đúng” xác nhận địa điểm; “đồng ý phí” xác nhận gói hỗ trợ; tạo đơn vẫn cần summary mới được xác nhận riêng. Area inquiry chỉ tính thử đến điểm đại diện, không promote thành điểm vận hành. Gói hỗ trợ mặc định tắt, chỉ sandbox; reviewed live registry phải có nguồn và kiểm chứng điểm tiếp cận.

Graph V2 có `interpret_turn → prepare_turn → process_turn`: interpreter/facts được checkpoint trước dispatch. Câu hỏi độc lập không ghi booking slots, scheduled issue hoặc consent. Dùng inquiry để đặt luôn làm lại summary/quote và đòi consent mới. Phiên v1 pending/unknown giữ đường tương thích cho đến kết quả xác định.

Sandbox một chiều, đặt ngay; hai ô tô bật mặc định, xe máy điện chỉ bật khi catalog/tariff/profile/chính sách đủ cấu hình. Chưa có driver/OTP/payment/nhiều workers. Cần holdout rộng trước khi cam kết hiểu mọi câu tiếng Việt. Trạng thái nghiệm thu: [MVP_STATUS.md](../../MVP_STATUS.md).

Nguồn: [Gemini generateContent](https://ai.google.dev/api/generate-content), [VietMap route v4](https://maps.vietmap.vn/docs/map-api/route-version/route-v4/), [Open-Meteo Forecast API](https://open-meteo.com/en/docs), [Open-Meteo terms](https://open-meteo.com/en/terms).
