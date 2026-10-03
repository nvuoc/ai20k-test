# Trạng thái MVP V3 — 03/10/2026

Đã triển khai lõi ParrotGo V3 theo [MVP_PLAN.md](MVP_PLAN.md), dùng chung cho chat web chỉ dùng văn bản và hàm `main(customer_text, session_id=...) -> str`. `main_async`/`TextBot` phục vụ tích hợp async, CLI nằm tại [src/backend/main.py](src/backend/main.py). ASR/TTS và điều phối xe thật chưa nằm trong phạm vi.

## Phạm vi của báo cáo

Đây là báo cáo hiện trạng, không phải một file kế hoạch thứ hai. Phạm vi và mục tiêu phát hành được chốt tại [MVP_PLAN.md](MVP_PLAN.md); versions tại [bảng contract](MVP_PLAN.md#contracts). Kết quả extractor, state graph và AssistantResponse HTTP là các đầu ra khác nhau. Số liệu development/live dưới đây không chứng nhận chất lượng production.

## Mở rộng V3 — 03/10/2026

- Groq `openai/gpt-oss-120b` chính, Gemini `gemini-3.5-flash-lite` dự phòng, strict output/evidence validation, tối đa hai provider calls mỗi processing attempt và deadline chung. Quota/cooldown request/token Groq lưu riêng; lỗi cấu hình chỉ degraded nếu cấu hình cho phép.
- State 6/turn 3/API 3/presentation 3 cho runtime mới; V2 interpretation đã checkpoint và giao dịch đang xử lý giữ đường tương thích. Migration draft yêu cầu consent địa điểm mới; không đổi transaction IDs/ledger.
- Địa điểm duy nhất hỏi xác nhận trực tiếp bằng lời bot. ACK, revision, policy, proposal binding và TTL chặn phản hồi cũ. Xác nhận địa điểm/phí không tạo đơn.
- Địa danh rộng hỏi địa chỉ chính xác; nếu không biết thì đề xuất điểm đại diện ổn định có nguồn và khả năng xe tiếp cận. Inquiry area chỉ là preview, không promote trực tiếp. Điểm đón trong khu vực rộng vẫn cần nơi đứng cụ thể.
- Gói hỗ trợ có giá/giới hạn sandbox, preview tuyến chưa cộng phí, consent phí riêng, breakdown và tổng tạm tính trong summary. Đổi vehicle/policy/hạn consent yêu cầu đồng ý phí lại. `AREA_ASSISTANCE_ENABLED=false` mặc định; chưa có điều phối hỗ trợ live.
- Route facts có thời gian/source/TTL/basis; congestion được đọc khi hỏi giải thích duration. Thiếu/stale/malformed traffic không làm mất route và không thành tuyên bố giao thông hiện tại hoặc ETA traffic-adjusted.
- Transcript chỉ render câu trả lời văn bản và tin nhắn khách. Bỏ cards, choice/confirm/cancel buttons, quick replies; giữ composer/new-trip ngoài chat, polling/cursor/ACK/retry và cuộn lịch sử.

Kiểm chứng ngày 03/10/2026: **546 backend tests đạt**, **15 browser tests đạt** (14 trong lượt đầy đủ, ca hỗ trợ đã sửa assertion và chạy lại; hai luồng inquiry/hỗ trợ được chạy lại sau guard mới). Ruff, typecheck/build và kiểm tra đồng bộ OpenAPI đều đạt. Demo `converse_text.py` chạy bằng văn bản qua xác nhận địa điểm, dùng tuyến hỏi thử, đặt/hủy đơn và thời tiết. Báo cáo: [verification-report.json](src/backend/evaluation/ver3/verification-report.json).

Đã gọi đúng một extraction thật cho từng provider: Groq `openai/gpt-oss-120b` khoảng 1,66 giây và Gemini `gemini-3.5-flash-lite` khoảng 1,59 giây, đều hợp lệ theo schema V3 gồm `location_decisions`; input là câu chào không chứa thông tin cá nhân, không gọi Maps/tạo đơn. Đây là kiểm tra transport/schema, chưa phải đánh giá holdout khả năng hiểu tiếng Việt. Các số liệu V2 bên dưới là bằng chứng lịch sử.

Cấu hình `.env` cục bộ đã chuyển sang Groq/Gemini, budget hai calls và bật xác nhận địa điểm; giữ nguyên khóa API. Hỗ trợ khu vực vẫn tắt mặc định vì phí/giới hạn đi kèm là sandbox và chưa có điều phối live. Browser tests bật hỗ trợ trong môi trường fixture riêng. Trên Windows, Playwright không tự tắt webserver sau suite; các Python server thử nghiệm đã được dừng đúng PID trước bàn giao. Khung VS Code được mô phỏng bằng viewport nhỏ và iframe, chưa kiểm tra trực tiếp webview VS Code của người dùng.

## Phần đã triển khai của V2 (lịch sử)

- ExtractorTurnResult (`contracts/turn.py:TurnResult`) tách booking acts, questions, inquiry actions và travel-party metadata; một inference mỗi lượt thông thường, FAQ/typed actions không inference. Evidence phải có trong văn bản đầu vào; chỉ sửa offset bằng literal duy nhất.
- Inquiry giữ ID/revision/TTL, địa điểm/candidate/tuyến/giá/giờ riêng. Hỏi C→D không sửa booking A→B hoặc lấy consent của summary cũ. Resume giữ thông tin; promote yêu cầu summary/quote và consent mới. UI hỗ trợ chọn xe, dùng tuyến và tiếp tục booking.
- Handler cho identity, fare, distance, catalog, pickup availability, weather, price objection và travel duration. Số liệu từ facts có scope/source; chưa có driver ETA thì nói rõ.
- Parse số nhà/ngõ/quan hệ có giữ raw text; một thực thể khớp bằng chứng được tự nhận. Resolve anchor trước câu hỏi điểm hẹn; không bịa tọa độ đối diện/cổng. Registry alias theo địa bàn, entity/source/version; dataset đi kèm là fixture, live không dùng alias fixture.
- Open-Meteo `/v1/forecast` thật và `GET /api/sessions/{id}/weather`. Kiểm tra timezone, đơn vị, coverage, binding và TTL; thiếu/lỗi nguồn không thành số 0. Thời tiết pickup có thể hỏi chỉ với một địa điểm; giờ đến là scenario theo route duration.
- Catalog thống nhất nhãn/sức chứa/tariff/profile giữa core, API và UI. Xe máy điện dùng profile `motorcycle`, có kiểm tra người lớn/trẻ em, mặc định chưa bật vì chưa có tariff/chính sách vận hành thực.
- State schema 5, turn/API/presentation/graph/policy V2. Checkpoint sau interpretation và prepare, trước effect. Migration tại ranh giới an toàn; pending/unknown v1 giữ flow tương thích và ID cũ. Quote hết hạn giữa prepare/dispatch đòi consent mới; ledger vẫn phục hồi commit cũ.
- Gemini quota 15 RPM dùng chung SQLite. `waiting_for_quota` giữ event qua restart, không tính là retry lỗi; model failure không fallback fixture. Inbox, render ACK, ingress guard, idempotency và unknown reconciliation được giữ.

## Kiểm chứng V2

- **409 pytest đã qua**: gồm 346 regression nền và 63 ca thêm cho V2; không dùng mạng hoặc key local trong tests.
- Frontend TypeScript/build, Ruff và generated OpenAPI contract đã qua. **6 browser E2E đã qua**, kiểm tra đặt/hủy, đổi xe khi xác nhận, candidate, mobile, inquiry/reload/chọn xe/promote và weather fixture.
- Live subset ngày 02/10/2026: Gemini `gemini-3.5-flash-lite`, VietMap v4 và Open-Meteo thật trả giá/km/duration/weather sau lựa chọn địa điểm thử nghiệm rõ ràng. Cả 3 lượt giữ booking slots trống và số đơn bằng 0. Một FAQ không gọi model; hai lượt tự do gọi Gemini. [Live V2 report](src/backend/evaluation/ver2/live-smoke-report.json).
- Lựa chọn candidate đầu tiên trong live smoke là lựa chọn có chủ ý để thử adapter, **không phải gold địa chỉ**. Dữ liệu thời tiết trong báo cáo là mẫu tại thời điểm kiểm tra, không phải dự báo luôn còn hiệu lực.
- Gemini thật diễn giải hợp lệ một câu đặt xe gồm 5 thông tin, không chứa điện thoại và không dispatch giao dịch. [Booking turn smoke](src/backend/evaluation/ver2/booking-turn-smoke-report.json). Các luồng đủ điện thoại/tạo/hủy V2 được kiểm thử offline; live subset này chưa là chứng nhận tạo/hủy end-to-end V2.
- Báo cáo V2 tách development/live/holdout; xem [verification-report.json](src/backend/evaluation/ver2/verification-report.json). Demo hàm main: [converse_text.py](src/backend/examples/converse_text.py).

## Giới hạn nghiệm thu V2

Chưa có holdout độc lập 160 ca hội thoại, 400 ca địa chỉ và 20 hội thoại như mục 16 của kế hoạch. Vì vậy chưa công bố đạt precision 99%, recall 80% hay hiểu ý định 95%. Cần bộ alias dân gian được review cho vùng đầu tiên; live giữ clarification khi thiếu bằng chứng. Xe máy điện cần cấu hình vận hành/tariff riêng trước khi bật. Giá và booking vẫn sandbox; chưa có driver/OTP/payment, đặt trước, nhiều điểm dừng, khứ hồi, sửa đơn đã tạo hoặc nhiều runtime dùng chung database.

Open-Meteo free dùng cho mục đích phi thương mại; có customer endpoint/key cho gói thương mại. Nguồn và điều kiện sử dụng được ghi ở [README](README.md).

## Số liệu V1 lịch sử — 01/10/2026

Các mục và số liệu dưới đây là báo cáo V1 lịch sử; không cộng chúng vào kết quả holdout V2.

Đã có ứng dụng local đầy đủ: giao diện chat tiếng Việt, API, worker, state/checkpoint bền vững, Gemini, VietMap, báo giá sandbox và tạo/xem/hủy đơn sandbox. Đơn thử nghiệm không gọi tài xế thật.

### Phần đã triển khai V1

- Giữ đủ 12 slot, thu thập nhiều thông tin mỗi lượt hoặc trả lời theo ngữ cảnh.
- Chuẩn hóa/validate liên hệ, capacity khách/hành lý; hỏi thêm khi đặt hộ hoặc sân bay có nguồn.
- Candidate có scope/revision/TTL; địa chỉ tương đối/mơ hồ cần làm rõ; thay cổng làm lại tuyến và giá.
- Giữ yêu cầu đặt trước, điểm dừng, khứ hồi và hỗ trợ ngoài phạm vi; khách phải rút/sửa rõ.
- Apply toàn lượt trước giao dịch; xác nhận kèm sửa/điều kiện/câu hỏi không tự tạo đơn.
- Guard độc lập với model cho snapshot, giá còn hạn, render evidence, sửa/hủy và ingress mới.
- Gemini native API, JSON schema subset và validator strict local; quota 15 RPM SQLite dùng chung cho mọi phiên/repair và tồn tại qua restart.
- SQLite inbox/transcript/cursor/ACK/projection đọc; LangGraph SQLite là state nghiệp vụ.
- Sandbox provider có create/lookup/cancel/idempotency; tự đối soát unknown với lịch bền vững tối đa 5 lượt.
- Node lỗi tạm thời resume cùng event; sau 3 lỗi kéo dài khóa phiên cần hỗ trợ, giữ checkpoint/giao dịch.
- Owner cookie ký, Origin guard, projection an toàn, client ID dedup, HTTP202 khác kết quả giao dịch.
- React responsive, typed buttons, polling, ACK, retry cùng ID, reload và chuyến mới.

### Bằng chứng kiểm chứng V1

- Pytest offline gồm 346 ca cho contracts, NLU, maps, domain, persistence, HTTP API và worker. Bộ tests chặn outbound network và không đọc key local.
- Hai ca crash dùng tiến trình thật dừng sau provider commit và sau checkpoint trước publication; resume không tạo thêm đơn hoặc lặp outcome.
- Browser E2E: nhập tự nhiên→tóm tắt→tạo→reload→hủy; xác nhận kèm đổi xe; địa điểm mơ hồ; màn hình mobile.
- Fixture regression gồm 72 lượt, 72 lượt hợp lệ/khớp annotation; đây là câu phát triển, chưa là holdout.
- Gemini smoke: 3/3 output hợp lệ, 3/3 đúng annotation khi chỉ chuẩn hóa các từ đồng nghĩa ASAP đã định nghĩa; khớp nguyên văn 1/3. Không đánh đồng literal match với hiểu sai thời gian.
- Gemini regression trước sửa prompt: 40 lượt, 39 output hợp lệ, 31 khớp nguyên văn, 33 khớp nếu chuẩn hóa riêng ASAP. Một output dùng mã xe ngoài catalog bị từ chối an toàn. Sau sửa prompt, chạy lại 7 ca có khác biệt: cả 7 hợp lệ, 6 khớp annotation; ca còn lại dùng provide_info thay change_info khi bổ sung số vali, domain vẫn cập nhật đúng giá trị. Chưa chạy lại cả bộ 40 sau sửa; không suy ra độ chính xác tổng mới từ subset này.
- VietMap thật: search/place v4 trả candidate có nguồn và route v4 trả khoảng cách/thời gian dương.
- API end-to-end dùng Gemini/VietMap thật đã tạo đúng một đơn sandbox, đọc lại mã đơn và hủy thành công. [Báo cáo](src/backend/evaluation/live-booking-report.json).

Báo cáo: [kiểm chứng bản bàn giao](src/backend/evaluation/verification-report.json), [fixture](src/backend/evaluation/fixture-report.json), [Gemini smoke](src/backend/evaluation/gemini-smoke-report.json), [Gemini 40 lượt](src/backend/evaluation/gemini-regression-report.json), [7 ca sau sửa prompt](src/backend/evaluation/gemini-targeted-report.json).

### Giới hạn tại baseline V1

- Chưa tích hợp hãng xe/driver/giá thật, thanh toán/OTP, đặt trước, nhiều điểm dừng, khứ hồi hoặc amendment đơn đã tạo.
- Chỉ chạy một process/worker. Nhiều instance cần scheduling/fencing và database dùng chung khác.
- Semantic review bằng code bao phủ các mẫu đã kiểm chứng. Chưa có đánh giá holdout độc lập ≥40 lượt hoặc chứng minh mức 90% tiếng Việt tự nhiên rộng; Gemini regression không thay thế nghiệm thu này.
- Single-node LangGraph có thể gọi lại NLU/phần đọc khi node bị ngắt. Provider ledger/lookup bảo vệ hiệu ứng giao dịch; không tuyên bố mỗi inference chạy đúng một lần sau crash.
- Tests kiểm tra nhiều cơ chế trong C001–C030, chưa là bộ artifact nghiệm thu độc lập đủ 30 kịch bản/tất cả biến thể 88 ca map. Các địa chỉ chưa có nguồn vẫn hỏi lại.
- Quota local kiểm soát request của MVP; ứng dụng khác cùng Google project cũng sử dụng quota phía Google.

Hướng dẫn chạy, địa điểm demo, backup và cấu hình: [README.md](README.md).


## Đồng bộ tài liệu và cấu trúc mã nguồn — 02/10/2026

- Mã nguồn đã gom vào src/backend, src/frontend và src/scripts; lệnh và liên kết dùng đường dẫn mới.
- MVP_PLAN.md hợp nhất nền chat/V2; các đặc tả mô tả contract hiện tại và đánh dấu hướng mở rộng.
- Map contract hiện tại là map-resolution-1, status resolved/ambiguous/not_found/unavailable, với place/candidates/binding.
- TurnInput/ExtractorTurnResult dùng parrotgo-turn-2; NluInput/NluResult còn phục vụ legacy. Phản hồi HTTP là AssistantResponse, main trả str.
- Đợt tổ chức mã nguồn đã chạy lại 409 pytest, 6 browser E2E, Ruff, generated API check và frontend build/typecheck. E2E cần dừng server con sau khi tests hoàn tất do cleanup Windows giữ tiến trình; runner sau đó thoát thành công.
- Đợt đồng bộ 8 tài liệu đã kiểm tra 175 liên kết nội bộ, 7 ví dụ JSON, schema/evidence và versions; tất cả đạt. Giữ đủ ma trận 80 ca extractor, 88 ca Map, 66 ca graph và 30 ca nghiệp vụ nền, cùng các tình huống V2 trong kế hoạch. Chỉ còn một MVP_PLAN.md; không bổ sung live eval hoặc bộ holdout mới.
