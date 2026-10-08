# Ghép mã chat vào repo P-016

Giải nén **nội dung ZIP trực tiếp vào thư mục gốc P-016** (nơi có `src/`,
`frontend/`, `scripts/`). ZIP không có lớp thư mục `parrotgo/` hay `P-016/` bao ngoài.
Mã được lấy từ working tree hiện tại, gồm cả thay đổi chưa commit.

## Bố trí theo CODEBASE_STRUCTURE.md

| Thành phần hiện tại | Đường dẫn trong P-016 |
| --- | --- |
| Graph, runtime, text adapter, coordinator | `src/modules/core/parrotgo_chat/` |
| Extractor, Groq/Gemini/OpenAI, quota, prompts | `src/modules/llm/parrotgo_chat/` |
| Map, quote cũ, booking sandbox, weather | `src/modules/tool/parrotgo_chat/` |
| Luật nghiệp vụ, xác nhận, địa chỉ, thời gian | `src/domain/parrotgo_chat/` |
| Pydantic contracts | `src/models/parrotgo_chat/` |
| Settings, inbox SQLite, CRM, KB, fixtures | `src/services/parrotgo_chat/` |
| HTTP API và app riêng | `src/api/parrotgo_chat/routes.py` |
| React/Vite, tests và bản giao diện đã build | `frontend/parrotgo_chat/` |
| Kiểm thử và đánh giá | `tests/parrotgo_chat/`, `eval/parrotgo_chat/` |
| Lệnh chạy và dependencies khóa hash | `scripts/parrotgo_chat/` |
| Tài liệu gốc và triển khai cũ để tham khảo | `src/parrotgo/parrotgo_chat/` |

Không thay `src/main.py`, `src/config.py`, `src/modules/*/api.py`, `frontend/index.html`,
`.env`, `requirements.txt`, `Dockerfile`, voice routes, LiveKit hoặc Speech của P-016.
Các parent package `__init__.py` của repo đích cũng không có trong ZIP.
Nếu đã từng ghép gói này, các file `parrotgo_chat` trùng tên sẽ được thay khi giải nén.
Manifest liệt kê từng đường dẫn và SHA-256. Chưa biết đường dẫn repo đích nên chưa
kiểm tra xung đột thực tế hoặc hợp đồng API/Core/Speech của repo đó.

## Chạy ngay sau khi giải nén

Yêu cầu Python 3.12 và tải dependencies lần đầu. Giao diện đã build sẵn, không
cần Node.js để chạy demo. Từ gốc P-016 trên Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/parrotgo_chat/run.ps1
```

Linux: `bash scripts/parrotgo_chat/run.sh`.
Mở `http://127.0.0.1:8001/`. Chọn cổng khác: `run.ps1 -Port 8002` hoặc `run.sh 8002`.
Môi trường Python riêng `.venv-parrotgo-chat`; cấu hình riêng `.env.parrotgo-chat`;
dữ liệu mới tự tạo trong `data/parrotgo_chat/`. Không chuyển secret hoặc SQLite cũ.
Mặc định `fixture_demo`, chạy thử offline không cần API key. Muốn gọi provider thật,
đổi `APP_PROFILE=chat_sandbox` rồi điền key trong `.env.parrotgo-chat`.
Không mở đồng thời nhiều runtime/process dùng cùng cặp database.

## Điểm nối với Core/Speech hiện có

Gói này thêm mã theo cây thư mục; giải nén không tự sửa luồng voice đang chạy.
Để gọi từ adapter hiện có, import entrypoint mới:

```python
from src.modules.core.parrotgo_chat.api import core_func

reply = await core_func(
    user_text, session_id,
    customer_phone="0901234567", customer_name="An",
)
```

Tên và SĐT bắt buộc ở lần đầu của phiên; các lượt sau có thể chỉ truyền text và
session_id. Với voice worker sống lâu, dùng `TextBot` như async context manager;
truyền `acknowledge_previous=False` cho `bot.ask` khi TTS chưa phát xong.
`core_func` trả chuỗi, có thêm kwargs riêng; không mặc định thay thế hàm Core cũ.
HTTP dùng `/api/...` như mã hiện tại, không tự chuyển thành `/api/v1/chat` của tài
liệu đích. STT/TTS, Speech, LiveKit và voice worker không có trong mã nguồn hiện tại.
Booking provider hiện tại vẫn là sandbox. Không tạo file giả cho chức năng thiếu.

CLI: `.venv-parrotgo-chat/Scripts/python.exe run_parrotgo_chat.py --phone 0901234567 --name An`.
Các lệnh CLI/HTTP mặc định đọc cấu hình chat riêng, không đọc `.env` của P-016.

## Phát triển và kiểm thử

```powershell
.venv-parrotgo-chat/Scripts/python.exe -m pip install --require-hashes -r scripts/parrotgo_chat/requirements.dev.txt
.venv-parrotgo-chat/Scripts/python.exe -m pytest -c scripts/parrotgo_chat/pytest.ini tests/parrotgo_chat -q
.venv-parrotgo-chat/Scripts/python.exe scripts/parrotgo_chat/generate_api_types.py --check
npm.cmd --prefix frontend/parrotgo_chat ci
npm.cmd --prefix frontend/parrotgo_chat run build
```

Vite proxy trỏ backend 8001. Playwright dùng Python riêng, hoặc biến
`PARROTGO_CHAT_PYTHON` để chỉ định Python 3.12 đã cài dependencies.
Tests module hiện có được chuyển import/đường dẫn cùng mã. Test triển khai VPS cũ
được giữ ở `original_deploy/test_setup_image.py` vì nó kiểm tra layout `src/backend`
cũ, không áp dụng cho gói ghép này. `original_docs` và `original_deploy` là bản gốc
để tra cứu, không phải lệnh triển khai cho layout mới.

## Quy tắc ứng xử

Đã đọc CODE_OF_CONDUCT.md: tôn trọng người đóng góp, góp ý vào code, giữ thông tin
riêng tư và báo cáo vi phạm kín. Bản gốc được giữ trong `original_docs/`.
