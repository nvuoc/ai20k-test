# Phản hồi văn bản và hướng mở rộng voice ParrotGo V2

Cập nhật: 02/10/2026. Runtime hiện tại là text/chat: code/domain chọn action, facts và template rồi trả AssistantResponse hoặc str. Tài liệu mô tả đường phản hồi đang có và giữ thiết kế voice/audio trong các mục mở rộng. Phạm vi duy nhất ở [MVP_PLAN.md](MVP_PLAN.md), state/consent ở [langgraph.md](langgraph.md), diễn giải ở [llmextractor.md](llmextractor.md), dữ liệu địa điểm ở [map.md](map.md).

Dự án đã có Python/Node lockfile và code V2. Chưa có Voice Worker, LiveKit/STT/TTS hay benchmark audio; các con số voice bên dưới là thông số thử nghiệm đề xuất, không phải kết quả hoặc SLA. Không có dependency tài liệu/schema bên ngoài workspace để hiểu các contract hiện tại.

## 1. Đường phản hồi đang chạy

| Việc | Thành phần hiện tại |
| --- | --- |
| Hiểu ý khách và scope | Gemini/fixture extractor → ExtractorTurnResult. |
| Hợp nhất booking/inquiry, validate facts và guard | ConversationEngine, ChatEngine và InquiryService. |
| Chọn hỏi/trả lời/confirm/pending/success | Policy/domain bằng code. |
| Dựng câu tiếng Việt từ facts | Template/nhánh phản hồi trong engine và inquiries. |
| Xuất kết quả | AssistantResponse/ChatSnapshot qua HTTP hoặc TextBot.ask/main trả str. |
| Xác nhận đã trình bày | Render ACK đúng response_id/generation. |

Nguồn [conversation.py](src/backend/app/domain/conversation.py), [engine.py](src/backend/app/domain/engine.py), [inquiries.py](src/backend/app/domain/inquiries.py) và [contracts/chat.py](src/backend/app/contracts/chat.py). Runtime chưa có hàm llm_planner_func, class ResponsePlan/RendererInput hoặc node render_response riêng. “Plan phản hồi” trong tài liệu chỉ action/facts/scope được domain chọn, không là model wire để client/model tự điền.

Mặc định một inference extractor mỗi lượt thường; FAQ allowlist/typed actions/ACK/polling có thể không gọi model. Không có một LLM thứ hai bắt buộc để quyết định câu hỏi hoặc diễn đạt câu trả lời. Budget 2 nếu caller bật chỉ phục vụ repair của adapter hiện tại, không tự cấp thêm renderer call.

## 2. Dữ liệu phản hồi

AssistantResponse gồm response_id, generation, text, action, focus, candidates, summary, presentation, booking_status, reason, booking và inquiry (nullable/default theo model). Text là string; không dùng shape final_response_text/allow_end_session của voice cũ làm JSON HTTP hiện tại.

PublicPresentation có contract_version chat-presentation-1/2, response_id, generation và các reference tùy trường hợp: prompt_id, snapshot_fingerprint, booking_revision, candidate_set_id, valid_until, scope_kind, scope_id, inquiry_revision. Phiên mới dùng chat-presentation-2; legacy 1 được giữ tương thích. Versions tại [bảng contract](MVP_PLAN.md#contracts).

Summary/presentation lấy từ state đã validate; client phải dùng reference server để confirm/select/promote. Giá/tên xe/nhãn/đơn vị từ catalog/facts, không invent enum hay tính lại fare ở UI. Public DTO không lộ checkpoint, raw provider payload, API key hoặc thông tin phiên khác.

TextBot/main trả text của response event đã publication, giữ session qua restart. Mặc định lượt sau ACK phản hồi trước; caller TTS tương lai phải trì hoãn ACK cho đến khi thật sự phát, không coi text đã sinh là đã nghe.

## 3. Nguyên tắc diễn đạt

1. Chỉ diễn đạt action/facts/scope đã được domain duyệt; không phân loại intent lại hoặc gọi tool từ renderer.
2. Câu hỏi giá/km/duration/weather dùng đúng inquiry hoặc committed booking được hỏi; không nhặt hai địa chỉ gần nhất rồi trả nhầm tuyến.
3. Apply hết sửa đổi/câu hỏi trước chọn template. “Đồng ý nhưng đổi xe” không dùng câu “đã đặt xe”.
4. Số tiền, thời gian, địa chỉ, số điện thoại và consent dùng giá trị literal đã kiểm tra; không làm mềm câu chữ thành một cam kết khác.
5. Thông báo thành công từ outcome đã tích hợp; booking sandbox chưa gọi tài xế thật, không nói tài xế đang tới hoặc ETA phút khi thiếu dispatch facts.
6. Nguồn lỗi/thiếu là chưa có dữ liệu, không là giá 0/không mưa/địa điểm chắc chắn không tồn tại.
7. Một câu hỏi chính khi phù hợp; tóm tắt consent vẫn phải đủ facts và scope. Đừng hỏi lại pickup_time nếu đã có đi ngay, hoặc bắt hỏi phone khi chỉ inquiry.
8. “Giá cạnh tranh” không là khẳng định mặc định khi không có bằng chứng thị trường.

Identity nói ParrotGo là trợ lý đặt xe sandbox. Catalog chỉ advertised/bookable từ runtime; xe máy điện mặc định tắt. Đi A→B bao lâu là route duration, khác thời gian tài xế đến. Weather fixture phải ghi là mẫu không phải dự báo thực tế.

## 4. ACK, stale và thay đổi giữa các lượt

Render ACK không là consent. Nó chứng minh response/generation đã trình bày cho UI, để domain kiểm tra prompt mà khách đang trả lời. ACK cũ/sai phiên/prompt superseded không cập nhật scope mới. Summary hết TTL hoặc fingerprint/revision khác phải lập lại trước create.

Candidate của inquiry không thay booking. Use-inquiry-route là promote có scope/TTL/revision/fingerprint, sau đó cần summary mới và consent riêng. Inquiry trả lời xong không tự phục hồi consent của booking cũ.

Nếu giá/slot/travel_party/điểm hẹn thay đổi trước dispatch, invalidate summary/consent phụ thuộc. Inbox ingress guard chặn effect cũ khi sửa/hủy đã đến trước dispatch. Poll/retry/publication dedup không phát thông báo success mới ở mọi lượt chỉ vì booking còn lưu.

## 5. Đo phản hồi và lỗi hiện tại

Đo ingress→receipt, queue wait, NLU, read facts, total turn→response publication/render và quota wait riêng. LLM TTFT không phải latency người dùng thấy; hiện chưa đo time-to-first-audio vì không có audio runtime.

LLM timeout mặc định 25s, read request 5s, nhánh đọc 15s. Quota wait có retry_at; event vẫn lưu, không fallback fixture. Fault retries/recovery theo [langgraph.md](langgraph.md). TextBot caller timeout không xóa inbox hoặc kết luận provider thất bại; retry cùng message_id.

Phần này là cấu hình/hành vi hiện có; số chất lượng/latency phải lấy từ báo cáo có sample và phiên bản, không dùng timeout làm SLA. [MVP_STATUS.md](MVP_STATUS.md) chỉ công bố development/live subset, holdout còn thiếu.

## 6. Hướng mở rộng voice — chưa triển khai

Voice giữ domain booking/inquiry và thêm Worker nhận audio, STT/endpointing ghép lượt final, TTS/playback và event delivery. LangGraph tiếp tục nhận văn bản/sự kiện nghiệp vụ; PCM, queue/timer và playback task không vào checkpoint nghiệp vụ.

Một domain writer mỗi phiên và một playback owner/actor. Worker phải dừng phát nhanh khi khách nói chen nhưng không tùy tiện hủy create/cancel đã dispatch; giao dịch outcome unknown vẫn phải đối soát. STT partial không đủ consent hoặc cho phép phát một câu hỏi chưa qua policy.

### 6.1. Router hybrid đề xuất

| Mode đề xuất | Khi dùng | Điều kiện |
| --- | --- | --- |
| static_audio | Câu cố định đã được policy chọn. | Asset có version, voice/locale hợp lệ. |
| template_tts | Câu có địa chỉ/giá/giờ/xe biến động. | Biến literal từ facts, cần TTS cho phần mới. |
| constrained_llm | Giải thích ít gặp, không quan trọng giao dịch. | Facts/action bất biến, output guard, cùng quota/deadline. |
| silent | Event không cần speech. | Không tạo TTS hoặc coi silence là delivery của summary. |

Template mặc định; consent/giá/điện thoại/booking outcome ưu tiên template literal. LLM renderer là experiment riêng, chưa phải dependency của MVP. Nếu bật, chia ngân sách tổng extractor/review/repair/renderer; không để mỗi adapter tự có 2 call. Quota Gemini của runtime không được vượt giới hạn chỉ vì thêm voice.

### 6.2. Template registry và audio cache đề xuất

Template cần ID/version/action, danh sách biến và mandatory spans; audio asset manifest cần template/asset version, voice/locale/format/duration/hash và quyền truy cập. Cache cố định khác cache câu có biến. Không cache PII bền vững chỉ để giảm TTS; không khóa consent vào một đoạn audio khác facts hiện tại.

Cache miss dùng TTS/template cùng plan; không phát asset stale hoặc sai voice rồi coi là đã đọc đúng summary. Nối các đoạn động phải giữ phát âm số tiền/địa chỉ và mạch câu; không suy consent từ filler hoặc chỉ một đoạn đầu summary.

### 6.3. Filler có điều kiện

Filler chỉ giảm im lặng cảm nhận, không tăng tốc tool. Đề xuất thử timer 400–600ms (ví dụ 450ms), clip ngắn khoảng 0,7–1,2s, tối đa một lần/lượt và cooldown 10s. Những con số này cần A/B bằng audio thật, chưa là default runtime.

Chạy tool/LLM đồng thời với timer, không đợi filler hết mới làm việc. Bỏ filler chưa phát khi main sẵn sàng; nếu đang phát, nối ở ranh giới phù hợp. Main có sớm thì không để filler làm khách nhận câu trả lời muộn hơn. Không lặp filler lúc quota chờ dài hoặc hứa “đã đặt” khi provider chưa có outcome.

Filler cần local cache hit, stage chậm có bằng chứng, generation còn active, khách không đang nói và còn budget hàng đợi. Nó không cập nhật last_bot_message/current_focus/pending consent như main prompt.

### 6.4. Queue, ngắt lời và streaming

Playback actor có state idle/filler_pending/filler_playing/main_ready/main_playing/interrupted/completed và một foreground queue. Timer/task gửi event vào actor; không await playback blocking khiến actor không xử lý user interruption.

Generation đổi khi lượt mới/supersede có chủ đích hoặc owner đổi, không đổi chỉ vì filler chuyển main. Cancel renderer/TTS/read task stale nếu cần; effect write đã dispatch phải giữ ledger/reconcile. False interruption cần policy endpointing riêng, không replay asset/summary theo phản xạ rồi tự xác nhận.

Đường đầu tiên nên validate hoàn chỉnh response trước TTS. Streaming theo unit là bước tiếp theo: unit text đã guard, seq bất biến, generation đúng, finite queue/backpressure, EOF đúng, mandatory consent spans đầy đủ. Worker nhận stream không TTS lại toàn final text gây lặp. Không coi token đầu tiên hoặc unit bị cắt là summary đã nghe xong.

### 6.5. Contract voice đề xuất

voice-presentation-1 là tên đề xuất cho presentation events/manifest của voice, chưa là HTTP contract hiện tại. VoiceOutput có thể cần final_response_text, bot_action, current_focus, booking_status/result và presentation reference; đây là tên mô tả mới, không class TurnResult của extractor.

Presentation cần session/turn/response/presentation IDs, generation, segment_id/unit_seq, event_id/event_seq và fencing token khi có nhiều playback owner. ACK thể hiện unit/presentation thực phát, interruption/cancel/completion và evidence của những mandatory spans đã phát; filler không là consent prompt.

Không đổi delivery_type=rendered của AckInput HTTP thành played mà chưa có schema/route/capability/version riêng. Pin Worker/SDK/voice/audio contracts và migration khi triển khai. Chỉ trả reference/manifest, không nhúng PCM vào state graph hoặc JSON public.

### 6.6. Tích hợp framework

LiveKit là một lựa chọn cho Voice Worker trong backlog; chưa có dependency/lockfile LiveKit trong ứng dụng. Khi triển khai phải chọn SDK đã pin, smoke audio/TTS/interruption/reconnect/ownership và đọc tài liệu chính thức cho version đó. Không coi pseudocode planner cũ là API framework đang được gọi.

## 7. Ma trận voice đề xuất

409 pytest/6 E2E hiện tại không chứng nhận voice, latency audio hoặc tất cả hàng dưới. Ma trận giữ yêu cầu thiết kế; cần tests riêng khi thực hiện roadmap, có outcome/state/evidence và số call mong đợi.


### 7.1. Ma trận kiểm thử tối thiểu

Các ca dưới là yêu cầu triển khai, chưa phải kết quả đã chạy trên hệ thống thật.

| ID | Tình huống | Kết quả phải giữ |
| --- | --- | --- |
| P01 | Hỏi passengers, static asset có sẵn | Không gọi LLM renderer/TTS; phát đúng mẫu, một lần |
| P02 | Câu có địa chỉ động | Template điền từ resolution; không dùng asset của địa chỉ khác |
| P03 | Renderer cần diễn đạt, còn budget | Chỉ nhận facts cần thiết; tối đa một lượt renderer |
| P04 | Extractor đã dùng hai lượt vì repair/review | Response dùng template; không lén gọi model thứ ba |
| P05 | Main audio ready trước ngưỡng filler | Timer bị hủy, không có filler |
| P06 | Timer và main_ready đến cùng vòng xử lý | Một quyết định tuần tự; không phát filler thừa hoặc hai luồng audio |
| P07 | Filler queued nhưng chưa phát | Main thay thế filler; không đọc filler sau main |
| P08 | Main về giữa filler, còn đoạn ngắn | Nối ở safe end, đo added_delay_by_filler |
| P09 | Main về giữa filler dài, có safe end | Dừng tại boundary đã duyệt, ghi handoff_to_main, main không bị cancel |
| P10 | Filler không có boundary gần | Không cắt mất từ chỉ để đạt soft target; ghi chậm thêm và đánh giá lại asset |
| P11 | Chỉ có token/text, TTS chưa ready | Không cắt filler rồi để khoảng trống do thiếu main frames |
| P12 | Nhiều stage chậm trong một turn | Tối đa một filler; phase mới không reset quota |
| P13 | Cache miss filler | Không TTS/LLM filler trên đường chậm; main tiếp tục |
| P14 | Filler đang phát, khách nói | Dừng/tạm dừng audio nhanh; cancel timer/queued filler, nghe khách |
| P15 | TTS cũ trả frame sau barge-in | Không enqueue/phát frame generation cũ |
| P16 | False interruption | Chỉ resume response còn hợp lệ, không mở create từ ACK giả |
| P17 | Khách nói “ừ” trong filler | Không lấy prompt cũ consumed/superseded để cấp booking consent |
| P18 | Khách sửa passengers trong filler | Giữ act sửa; snapshot/quote/capacity được đánh giá lại |
| P19 | Filler completed ACK | Không đánh dấu pending prompt chính delivered |
| P20 | Summary mất một segment nhưng câu hỏi cuối phát | Không chốt scope đầy đủ; hỏi lại phần cần thiết |
| P21 | Main response đã phát qua units, sau đó VoiceOutput hoàn tất | Không TTS/phát lại toàn bộ text lần nữa |
| P22 | Custom progress event replay | Không phát thêm filler trong cùng logical turn |
| P23 | Worker restart sau khi có thể đã phát một phần | Không tự replay toàn response; đối chiếu delivery, conservative với consent |
| P24 | LLM renderer bịa ETA/giá | Không phát phần sai; template fallback hoặc làm rõ |
| P25 | LLM token từ extractor lọt vào stream chung | Bridge chặn; không đọc JSON/notes nội bộ ra loa |
| P26 | Quote hết hạn khi main còn chờ sau filler | Không phát/chốt giá cũ; refresh và lấy lại đồng ý khi cần |
| P27 | Chỉ đổi liên hệ sau khi đã render summary | Audio summary cũ mất quyền dùng; không ảnh hưởng lookup độc lập không liên quan |
| P28 | Create đã dispatch rồi khách ngắt | Không coi cancel audio là cancel booking; ledger/reconcile tiếp tục |
| P29 | Callback booking thành công thuộc lượt cũ | Lưu kết quả nghiệp vụ; thông báo mới phải do policy hiện hành cho phép |
| P30 | TTS EOF nhưng còn audio trong output buffer | Chưa phát completed ACK/consent sớm |
| P31 | Audio format/voice profile sai | Không phát giọng/format sai; chuyển fallback đúng cấu hình |
| P32 | Cache câu cá nhân của session khác | Không sử dụng; kiểm tra scope và retention |
| P33 | SDK tự generate reply trong khi graph cũng trả lời | Integration test phát hiện và cấu hình một owner; không có hai câu chồng |
| P34 | Tool/renderer rất chậm sau filler | Inform pending/fallback đúng phase, không lặp filler vô hạn |
| P35 | Speech noise/no-speech hoặc khách chưa hết lượt | Không phát filler như đã nhận một yêu cầu hoàn chỉnh |
| P36 | Người dùng đồng ý sau summary hợp lệ, đủ delivery/guard | Đúng một create; không đạt “an toàn” bằng cách chặn mọi booking |

### 7.2. Fault/race và chỉ số

Thử main về trước/trong/sau filler, cache miss, TTS lỗi giữa unit, stale generation, barge-in/false interruption, reconnect/owner failover, quote hết hạn và sửa slot lúc queue chờ. Giao dịch đã dispatch và effect commit phải đối soát, không cancel task làm mất outcome.

Đo time-to-any-audio, time-to-meaningful-audio, response completion, audio overlap/gap, stale unit phát ra, filler frequency/delay, duplicate TTS, mandatory-span completion và model/TTS/tool cost. Có baseline/nhóm đối chứng và audio thật; không lấy proposal 450ms làm kết quả đã đạt.

## 8. Triển khai từng bước

1. Giữ template text và domain guards hiện tại; củng cố holdout V2 theo MVP_PLAN.
2. Thêm Voice Worker/STT/TTS, final turn input và playback ACK/version riêng; kiểm chứng consent thực nghe.
3. Thêm static audio registry/cache, đo baseline và lỗi cache.
4. Thêm filler actor có điều kiện, thử queue/interruption/race trước canary.
5. Chỉ thử LLM renderer hoặc validated-unit streaming khi quota, facts guard, latency và speech evidence đạt bộ riêng.

Các bước đều là backlog chưa triển khai. Khi thay response shape/state/topology/consent phải cập nhật schema, version/capability, migration, fixture, docs và Worker cùng lúc. Kế hoạch sản phẩm và tiêu chí phát hành chỉ duy trì trong [MVP_PLAN.md](MVP_PLAN.md).
