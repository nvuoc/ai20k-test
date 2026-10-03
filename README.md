# ParrotGo — MVP V3, văn bản khách → văn bản bot

React + TypeScript, FastAPI, LangGraph và SQLite. Groq `openai/gpt-oss-120b` diễn giải lời khách, Gemini `gemini-3.5-flash-lite` dự phòng; VietMap tìm địa điểm/tuyến; Open-Meteo cung cấp dự báo. Cùng một lõi phục vụ giao diện hội thoại và hàm Python nhận/trả văn bản. Giá và đơn `SBX-` là sandbox, chưa điều phối tài xế thật.

V2 tách **tuyến hỏi thử** khỏi **chuyến đang đặt**. Khách có thể hỏi giá/km/thời gian/thời tiết của C→D trong khi đặt A→B, chọn địa điểm/xe cho tuyến hỏi thử, tiếp tục A→B hoặc dùng C→D để lập tóm tắt mới. Chỉ xác nhận riêng tóm tắt mới mới tạo đơn. Thiết kế: [MVP_PLAN.md](MVP_PLAN.md); kết quả kiểm chứng: [MVP_STATUS.md](MVP_STATUS.md).

## Cấu trúc dự án

```text
src/
  backend/     # API Python, lõi hội thoại, tests và ví dụ
  frontend/    # Giao diện React + TypeScript và tests
  scripts/     # Khởi tạo cấu hình và kiểm tra dữ liệu demo
README.md
MVP_PLAN.md    # Kế hoạch MVP duy nhất
MVP_STATUS.md  # Kết quả kiểm chứng và giới hạn
```

Các lệnh bên dưới chạy từ thư mục gốc dự án. Cấu hình cục bộ và dữ liệu backend nằm tại `src/backend/.env` và `src/backend/data/`.

## Hàm chính nhận và trả văn bản

Chạy từ thư mục `src/backend`, hoặc cài package backend vào môi trường Python:

```python
from app.text import main

reply: str = main(
    "Từ Nhà hát Lớn Hà Nội đến Ga Hà Nội giá bao nhiêu?",
    session_id="khach-001",
)
print(reply)
reply = main("Đi bao lâu?", session_id="khach-001")
```

Dùng lại `session_id` để giữ ngữ cảnh qua nhiều lượt và restart; đổi ID để mở hội thoại mới. `message_id` tùy chọn phải giữ nguyên khi retry cùng đầu vào. Hàm trả **lời bot**, có thể đưa sang TTS; lời khách đã chuyển từ âm thanh sang text là đầu vào. ASR/TTS không nằm trong MVP.

Ứng dụng async dùng `await main_async(text, session_id=...)`. Dịch vụ chạy lâu nên giữ một `TextBot`:

```python
from app.text import TextBot

async with TextBot() as bot:
    reply = await bot.ask("Bạn là ai?", session_id="khach-001")
    reply = await bot.ask("Có những loại xe nào?", session_id="khach-001")
```

Mặc định lượt kế tiếp xác nhận lời bot trước đã được nhận. Với TTS chưa phát xong, truyền `acknowledge_previous=False` vào `bot.ask`; bot vẫn kiểm tra lời xác nhận đúng tóm tắt và hiệu lực giá trước khi tạo đơn. Text adapter dùng `data/text.sqlite` và `data/text_checkpoints.sqlite`, tách khỏi phiên HTTP; quota Gemini vẫn dùng chung. Chạy một runtime/process cho mỗi cặp database.

CLI từ thư mục dự án:

```powershell
src/backend/.venv/Scripts/python.exe src/backend/main.py "Bạn là ai?" --session khach-001
src/backend/.venv/Scripts/python.exe src/backend/main.py --session khach-001
src/backend/.venv/Scripts/python.exe src/backend/examples/converse_text.py
```

Lệnh cuối chạy demo offline trong database tạm, gồm hỏi tuyến khác, dùng tuyến, xác nhận và hủy.

## Chạy trên Windows

Yêu cầu Python 3.12, Node 22.12+ và uv. Từ thư mục dự án:

```powershell
uv sync --project src/backend --extra dev --locked --cache-dir .cache/uv
src/backend/.venv/Scripts/python.exe src/scripts/init_env.py
npm.cmd --prefix src/frontend ci --cache .cache/npm
npm.cmd --prefix src/frontend run build
src/backend/.venv/Scripts/python.exe src/scripts/seed_demo.py
src/backend/.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir src/backend --host 127.0.0.1 --port 8000 --workers 1
```

Mở **http://127.0.0.1:8000**. Backend phục vụ giao diện đã build cùng origin. Linux/macOS thay `Scripts/python.exe` bằng `bin/python` và `npm.cmd` bằng `npm`.

`src/scripts/init_env.py` tạo `src/backend/.env` nếu chưa có và giữ nguyên key hiện có. Cấu hình trong file này:

```dotenv
APP_PROFILE=chat_sandbox
LLM_PROVIDER=groq
GROQ_API_KEY=your-key
GROQ_MODEL=openai/gpt-oss-120b
LLM_FALLBACK_ENABLED=true
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_RPM=15
MAX_LLM_CALLS_PER_TURN=2
LOCATION_CONFIRMATION_ENABLED=true
TRAFFIC_ENABLED=true
AREA_ASSISTANCE_ENABLED=false
MAPS_PROVIDER=vietmap
VIETMAP_API_KEY=your-key
BRAND_NAME=ParrotGo
WEATHER_PROVIDER=open_meteo
OPEN_METEO_API_KEY=
```

Key chỉ ở backend; `.env.example` không chứa key thật. `APP_SECRET` được tạo và lưu bền vững tại thư mục data nếu chưa cấu hình. Giữ secret và database để khôi phục cookie/phiên sau restart.

Chạy demo offline không gọi API:

```powershell
$env:APP_PROFILE="fixture_demo"
src/backend/.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir src/backend --host 127.0.0.1 --port 8000 --workers 1
```

Gỡ override để dùng cấu hình Groq/Gemini: `Remove-Item Env:APP_PROFILE`. `chat_sandbox` không tự đổi sang fixture khi lỗi key/quota. Có thể đặt `MAPS_PROVIDER=fixture` để thử model với bộ địa điểm mẫu.

## Dùng thử

> Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, đi ngay, 2 người, xe 4 chỗ, số 0901234567.

Bot hỏi xác nhận trực tiếp từng địa điểm duy nhất bằng văn bản. Trả lời “đúng” cho nơi đang được hỏi; sau đó bot trình bày tóm tắt và giá, khách trả lời “đồng ý đặt xe” để tạo đơn thử nghiệm. Mọi lựa chọn trong đoạn chat đều bằng văn bản; nhiều địa điểm được đánh số để khách trả lời “cái thứ hai” hoặc tên địa điểm. Khách cũng có thể nhắn “hủy đơn” hoặc “tiếp tục chuyến đang đặt”. Giao diện giữ vị trí khi cuộn lên đọc lịch sử.

Địa danh rộng như Ocean Park 1/thôn Lai Xá được hỏi địa chỉ cụ thể trước. Nếu không biết, bot đề xuất điểm đại diện có nguồn để tính tuyến; điểm đón vẫn cần nơi đứng cụ thể. `AREA_ASSISTANCE_ENABLED=true` chỉ bật gói **hỗ trợ sandbox** có giá/giới hạn tại `app/fixtures/assistance_policy.json`; phí mẫu 20.000/15.000 đ không phải giá vận hành. Khách đồng ý hỗ trợ sẽ thấy giá tuyến tạm chưa cộng phí, rồi xác nhận phí riêng và xác nhận đơn riêng. Live cần registry điểm đại diện đã kiểm chứng tại `SERVICE_AREA_PATH`; dữ liệu fixture không được đưa vào bản đồ live.

Sau khi hỏi thời gian tuyến, khách có thể hỏi “sao đi lâu thế?”. Bot giải thích bằng facts của đúng tuyến vừa trao đổi. Chỉ báo tình trạng giao thông hiện tại khi nguồn trả dữ liệu hợp lệ còn hạn; chưa có bằng chứng ETA đã hiệu chỉnh giao thông thì bot nói rõ thời gian là ước tính từ nhà cung cấp.

Sáu thông tin cần có: điểm đón, điểm đến, đi ngay, số người, loại xe và điện thoại. Đặt hộ hỏi thêm tên người đi. Chuyến có sân bay được xác minh hỏi hành lý; sức chứa xe/hành lý được kiểm tra. Xe sandbox 4 chỗ tối đa 4 khách, xe 7 chỗ tối đa 6 khách. Điện thoại kiểm tra định dạng, chưa xác thực sở hữu.

Đặt trước, khứ hồi, nhiều điểm dừng, ghế trẻ em/xe lăn/thú cưng và sửa đơn đã tạo chưa được hỗ trợ. Hỏi thử giá/thời tiết ngày mai không thay đổi thời gian đặt xe. Địa chỉ có một thực thể khớp bằng chứng được nhận và đọc lại; địa chỉ tương đối được tìm mốc rồi hỏi chi tiết điểm hẹn, không suy tọa độ phía đối diện/cổng khác.

## Thời tiết Open-Meteo

Adapter gọi `https://api.open-meteo.com/v1/forecast`, đọc dữ liệu theo giờ: nhiệt độ °C, mã WMO, xác suất mưa %, lượng mưa mm và gió km/h. Kèm nguồn, thời gian lấy dữ liệu, thời hạn cache 15 phút, coverage và timezone `Asia/Ho_Chi_Minh`. Thời gian mơ hồ được hỏi lại; ngoài coverage/lỗi nguồn không dùng số liệu giả. Mưa thuộc khoảng giờ dự báo, không phải đo tại đúng phút lên xe. Xem [Open-Meteo Forecast API](https://open-meteo.com/en/docs).

Trong HTTP có endpoint đọc, không sửa chuyến:

```text
GET /api/sessions/{session_id}/weather?target=pickup&at=2026-10-03T08:00:00%2B07:00
```

`target=pickup|destination`, `at` bắt buộc timezone; `inquiry_id` tùy chọn lấy điểm của tuyến hỏi thử. Endpoint chỉ dùng địa điểm đã resolve của phiên thuộc owner cookie. Trong hội thoại chỉ cần nói “Thời tiết ở Nhà hát Lớn Hà Nội bây giờ có mưa không?”; một địa điểm là đủ.

`fixture_demo/test` trả mẫu có nhãn “không phải dự báo thực tế”; `WEATHER_PROVIDER=disabled` tắt hẳn. Có `OPEN_METEO_API_KEY` thì dùng customer endpoint. API miễn phí dành cho mục đích phi thương mại; sử dụng thương mại cần gói phù hợp theo [điều khoản Open-Meteo](https://open-meteo.com/en/terms).

## Catalog và dữ liệu địa phương

Xe máy điện có mã `xe_may_dien`, mặc định **chưa bật**. `VEHICLE_CATALOG_PATH` và `PRICING_PATH` có thể trỏ tới catalog/tariff đã xác minh: cần giá riêng, profile `motorcycle`, giới hạn khách/hành lý và chính sách trẻ em rõ ràng. Profile tuyến VietMap dùng `motorcycle`, không lấy tuyến ô tô rồi đổi nhãn. Runtime từ chối cấu hình bật xe mà thiếu dependency; frontend đọc loại xe/nhãn/sức chứa từ backend.

`LOCAL_ALIAS_PATH` trỏ tới registry có `version`, `notice`, `entries`; mỗi entry chứa `alias`, `area`, `canonical_query`, `entity_id`, `source_ref`, `source_version`, `kind=reviewed|fixture`. Alias mơ hồ cần địa bàn; nguồn/version giữ trong place metadata, không có tọa độ tự dựng. Registry đi kèm chỉ là fixture; live bỏ qua entry `kind=fixture`. Chưa có bộ alias dân gian được review cho một vùng vận hành thực.

## Địa điểm demo offline

20 điểm, 34 tuyến có hướng, có version và nguồn thử nghiệm. Các tuyến thuận tiện:

| Điểm đón | Điểm đến |
| --- | --- |
| Nhà hát Lớn Hà Nội | Ga Hà Nội |
| Nhà hát Lớn Hà Nội | Bạch Mai cổng chính / Bạch Mai cổng sau |
| Nhà hát Lớn Hà Nội | Nội Bài T1 cửa 3 / Nội Bài T2 cửa 2 |
| 15 ngõ 20 Nguyễn Trãi | Ga Hà Nội / Nội Bài T1 cửa 3 |
| Chợ Bến Thành | Tân Sơn Nhất T1 D1 / Tân Sơn Nhất T2 A1 |
| Chợ Bến Thành | Bệnh viện Chợ Rẫy |
| 123/45/6 Nguyễn Đình Chiểu | Tân Sơn Nhất T1 D1 |

“Trường Sao Mai” đưa ra hai cơ sở; “Bạch Mai” đưa ra hai cổng. Điểm resolve được nhưng không có tuyến seed vẫn chưa báo giá; không dùng khoảng cách đường thẳng để giả tuyến xe. Với VietMap, search/place/route dùng v4; adapter hỗ trợ tùy chọn v3 cho search/place theo gói key.

## Groq và Gemini dự phòng

Mỗi lần xử lý gọi tối đa một Groq và một Gemini dự phòng, dùng chung deadline 25 giây; FAQ có allowlist không gọi model. Groq có quota RPM/token, Gemini tối đa 15 RPM local; SQLite chia sẻ mọi phiên và giữ cooldown qua restart. Mỗi request thực đều chiếm quota. Chờ quota local giữ input trong inbox và không tiêu hao retry lỗi; lỗi quota từ HTTP có counter riêng giới hạn ba lần. Lỗi transport có retry hữu hạn; lỗi cấu hình/auth/model dừng để hỗ trợ thay vì bắt khách viết lại. Giới hạn thực tế cần cấu hình theo tài khoản từng provider.

Google tính quota theo project. Request từ ứng dụng khác dùng cùng project cũng sử dụng quota đó; bộ giới hạn local chỉ kiểm soát request của MVP này. [Google rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

## Lưu trữ và phục hồi

`src/backend/data/app.sqlite`: inbox, lịch sử/cursor, ACK, projection đọc, lịch đối soát và provider sandbox. `src/backend/data/checkpoints.sqlite`: trạng thái nghiệp vụ LangGraph. `src/backend/data/gemini_rate.sqlite`: quota/cooldown. Runtime data nằm ngoài Git.

Tin nhắn trả 202 sau khi lưu inbox. Worker xử lý tuần tự mỗi phiên. Retry cùng ID/cùng nội dung đọc kết quả cũ; khác nội dung trả 409. Tóm tắt gắn revision/fingerprint/TTL và evidence đã render. Lời sửa vào inbox trước dispatch chặn thao tác tạo cũ.

Graph V2 checkpoint sau diễn giải và sau chuẩn bị facts, trước tạo/hủy. Crash sau diễn giải không cần gọi model lại; crash trước checkpoint này vẫn có thể tốn một inference mới. Sandbox ledger phục hồi đơn đã commit, kể cả giá cũ hết hạn, ngăn tạo thêm đơn. Lỗi node tạm thời resume có giới hạn; lỗi kéo dài khóa phiên cần hỗ trợ và giữ checkpoint. Không chạy nhiều workers/instance dùng chung database.

Phiên cấu hình mới pin `parrotgo-turn-3`, `chat-api-3`, `chat-presentation-3`, state schema 6, graph/policy 3. Mười hai slot giữ `booking-slots-3`; map adapter giữ `map-resolution-1` với area/route/traffic facts bổ sung. Phiên cũ chỉ migrate tại ranh giới lượt an toàn; interpretation V2 đã checkpoint và giao dịch pending/unknown/booked giữ flow tương thích, không đổi draft/booking/idempotency IDs. Khi migrate draft, xác nhận địa điểm và tóm tắt cần được hỏi lại theo policy mới.

Backup: dừng backend, sao lưu cả `src/backend/data/` gồm secret/database; khởi động lại dùng nguyên cấu hình. Không xóa database có giao dịch chưa rõ. Thử môi trường sạch bằng đường dẫn database mới, giữ dữ liệu cũ.

## Kiểm thử

```powershell
src/backend/.venv/Scripts/python.exe -m pytest src/backend/tests -q --basetemp .cache/pytest-run
src/backend/.venv/Scripts/ruff.exe check src/backend/app src/backend/tests src/backend/examples src/backend/scripts src/scripts
src/backend/.venv/Scripts/python.exe src/backend/scripts/generate_api_types.py --check
npm.cmd --prefix src/frontend run typecheck
npm.cmd --prefix src/frontend run build
cd src/frontend
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

Tests dùng profile test/database tạm và chặn outbound network. Browser E2E dùng server riêng cổng 8001/database `.cache/e2e`. Fault tests có tiến trình con dừng thực sau provider commit hoặc sau checkpoint trước publication.

```powershell
src/backend/.venv/Scripts/python.exe src/backend/examples/evaluate_nlu.py --help
src/backend/.venv/Scripts/python.exe src/backend/examples/extract_chat.py --live
src/backend/.venv/Scripts/python.exe src/backend/examples/smoke_booking.py
src/backend/.venv/Scripts/python.exe src/backend/examples/smoke_v2.py --live --report src/backend/evaluation/ver2/live-smoke-report.json
```

Fixture eval là regression của các câu đã phát triển, không chứng minh chất lượng LLM. Kết quả API thật và giới hạn nghiệm thu xem [MVP_STATUS.md](MVP_STATUS.md).

## Phát triển

Backend chạy như trên; terminal thứ hai `npm.cmd --prefix src/frontend run dev`, mở http://127.0.0.1:5173. Vite proxy `/api` về 8000. Request ghi kiểm tra Origin; cookie signed/HttpOnly/SameSite. Chưa có OTP/tài khoản.

[MVP_PLAN.md](MVP_PLAN.md) là kế hoạch MVP duy nhất, hợp nhất nền chat và hội thoại V2. [Backend README](src/backend/README.md) có chi tiết API/adapter. Holdout 160 ca hội thoại, 400 ca địa chỉ và 20 hội thoại nghiệm thu độc lập trong V2 vẫn là việc đánh giá tiếp theo; các regression/smoke phát triển không thay thế chúng.


## Bộ tài liệu

| Tài liệu | Nội dung |
| --- | --- |
| [MVP_PLAN.md](MVP_PLAN.md) | Phạm vi, versions, lộ trình và mục tiêu nghiệm thu. |
| [langgraph.md](langgraph.md) | State, ba node V2, consent, ledger và recovery. |
| [llmextractor.md](llmextractor.md) | TurnInput và ExtractorTurnResult; NluInput/NluResult chỉ cho legacy. |
| [map.md](map.md) | MapResolution hiện tại, địa điểm, tuyến, alias và giới hạn dữ liệu. |
| [llmplanner.md](llmplanner.md) | Phản hồi văn bản và hướng mở rộng voice/audio. |
| [MVP_STATUS.md](MVP_STATUS.md) | Bằng chứng development/live và giới hạn chưa nghiệm thu. |

Versions được chốt tại [bảng contract](MVP_PLAN.md#contracts). ExtractorTurnResult là bí danh tài liệu cho class TurnResult trong contracts/turn.py; phản hồi HTTP là AssistantResponse. Map status thành công là resolved, trường địa điểm là place; dữ liệu chi tiết theo model đang thực thi. Các ví dụ/fixture không phải dữ liệu vận hành thật.
