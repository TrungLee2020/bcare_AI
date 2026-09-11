# Phase 5 — SSE & Resilience

## ⚠️ Chưa có xác thực

`GET /chat/stream?user_id=...` lấy `user_id` thẳng từ query param. Nghĩa là ai
cũng nghe được câu trả lời của người khác nếu đoán được `user_id` — mà user_id
là số nguyên tăng dần.

Service này được thiết kế để chạy **sau** gateway/BE hiện có (nơi đã xác thực
người dùng) và **không được expose thẳng ra internet**. Trước khi rollout phải
thay `user_id` bằng danh tính lấy từ token. Đây là nội dung sức khoẻ, nên đây là
việc chặn rollout, không phải việc "làm sau nếu kịp".

## Vì sao response consumer dùng consumer group RIÊNG cho mỗi instance

Đây là điểm dễ làm sai nhất của phase này.

| Topic | Group | Ngữ nghĩa |
|---|---|---|
| `chat_requests` | **chung** cho mọi instance | CHIA việc — mỗi message chỉ 1 instance xử lý |
| `chat_responses` | **riêng** mỗi instance (`...-sse-<random>`) | PHÁT TỚI TẤT CẢ |

Kết nối SSE của một user nằm ở đúng một instance cụ thể, và không instance nào
biết trước là instance nào. Nếu `chat_responses` cũng dùng chung group, response
sẽ rơi vào một instance bất kỳ — nhiều khả năng không phải instance đang giữ kết
nối của user đó, và câu trả lời không bao giờ tới nơi.

Cái giá: mọi instance đọc mọi response. Chấp nhận được ở quy mô hiện tại
(response nhỏ, lượng thấp). Muốn tiết kiệm hơn thì chuyển sang sticky routing
theo `user_id`, hoặc fanout qua Redis pub/sub.

Group này sinh ra cùng process và chết cùng process nên dùng
`auto_offset_reset="latest"`: đọc lại từ đầu topic là vô nghĩa (toàn response cũ
của những kết nối đã đóng). Phần đã lỡ được lo bằng replay buffer.

## Reconnect: `last_request_id`

Mất mạng giữa chừng là chuyện bình thường trên mobile. Message Kafka đã được
consume và commit rồi — không ai phát lại nữa. Nên response được giữ thêm trong
một bộ đệm Redis (`stream:{user_id}`, list có giới hạn + TTL).

```
GET /chat/stream?user_id=42&last_request_id=<id cuối cùng client nhận được>
```

Server phát lại mọi response sau mốc đó rồi mới stream tiếp realtime. Client lấy
`last_request_id` từ trường `id:` của khung SSE.

**Không tìm thấy `last_request_id`** (offline quá lâu, id lạ) thì phát lại toàn
bộ bộ đệm: thà gửi thừa còn hơn để mất câu trả lời. FE dedup bằng `request_id`.

Event `processing` **không** được lưu vào bộ đệm — phát lại một thông báo "đang
xử lý" đã cũ chỉ làm client hiểu nhầm.

## Timeout: event `processing`

Quá `SSE_PROCESSING_NOTICE_SECONDS` mà chưa trả lời xong thì consumer đẩy một
response `status="processing"` để client biết hệ thống vẫn đang chạy. Nó đi qua
đúng topic `chat_responses` như mọi response khác, vì kết nối SSE của user có
thể đang ở instance khác.

`SSE_PROCESSING_NOTICE_SECONDS` **phải nhỏ hơn** `OPENAI_TIMEOUT_SECONDS` — gửi
"đang xử lý" sau khi đã timeout thì vô nghĩa. Có test giữ ràng buộc này, cho cả
giá trị mặc định lẫn giá trị trong `.env.example`.

Ngoài ra stream gửi `: keepalive` mỗi `SSE_HEARTBEAT_SECONDS`, và đặt header
`X-Accel-Buffering: no` — thiếu header này nginx sẽ gom event lại rồi đẩy một
lượt, SSE mất hết tính realtime.

## Retry & dead-letter

`app/services/retry.py`: exponential backoff + **jitter**, chỉ retry lỗi tạm
thời (`RateLimitError`, `APITimeoutError`, `APIConnectionError`,
`InternalServerError`).

- **Jitter là bắt buộc**: khi OpenAI rate-limit thì TẤT CẢ consumer dính cùng
  lúc; không jitter thì chúng cùng ngủ đúng bằng nhau rồi cùng thức dậy đập vào
  API một lượt và lại bị rate-limit tiếp.
- **Không retry lỗi vĩnh viễn** (sai API key, request không hợp lệ): chỉ làm
  chậm rồi vẫn hỏng, mà message bị giữ trong partition lâu hơn nên các user khác
  trong cùng partition phải chờ theo.

Hết số lần thử → đẩy sang `chat_requests_dlq`:

| reason | Khi nào |
|---|---|
| `invalid_schema` | message không parse được (retry vô nghĩa, không bao giờ đúng ở lần sau) |
| `openai_failed` | đã retry hết `OPENAI_MAX_ATTEMPTS` |

DLQ giữ `payload` dạng **chuỗi thô**: message vào đây thường là vì không parse
được, ép nó về schema lần nữa sẽ mất đúng phần cần điều tra.

Vào DLQ vẫn có: hoàn quota + publish response `status="error"` để SSE không treo
chờ vô hạn.

## Hàng đợi SSE có giới hạn

Mỗi kết nối có `asyncio.Queue(maxsize=50)`. Client chậm (mạng yếu, tab treo)
không được phép làm phình bộ nhớ vô hạn — đầy thì bỏ event **cũ nhất**. Event bị
bỏ vẫn lấy lại được bằng replay khi reconnect.

## Test

```bash
pytest -q    # 136 test
```

Test SSE gọi thẳng `event_stream` chứ không qua httpx: `ASGITransport` gom toàn
bộ body rồi mới trả về, nên một stream không có điểm kết thúc sẽ treo mãi. Muốn
test qua HTTP thật thì phải chạy uvicorn — quá nặng cho unit test.

**Chưa được kiểm:** hành vi qua nginx/LB thật (buffering, timeout kết nối idle),
và fanout khi chạy nhiều instance thật với Kafka thật. Cả hai cần môi trường
staging — xem Phase 6.

## Còn nợ

- Xác thực cho `/chat/stream` (chặn rollout, xem đầu file).
- Chưa có công cụ replay message từ DLQ về lại `chat_requests`.
- Chưa có metric/alert cho: tỉ lệ vào DLQ, số lần retry, số kết nối SSE đang mở.
