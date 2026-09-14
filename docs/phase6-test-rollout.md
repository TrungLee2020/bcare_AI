# Phase 6 — Test & Rollout

## Hai lỗ hổng đã đóng ở phase này

### 1. `tier` và `user_id` đến từ client

Trước Phase 6, `POST /chat/ask` đọc `user_id` và `tier` từ **body do client
gửi**, còn `GET /chat/stream` đọc `user_id` từ **query param**. Hệ quả:

- Gửi `tier: "premium"` là được 5 câu/ngày thay vì 2. Quota chỉ là gợi ý.
- Mở SSE bằng `user_id` của người khác là nghe được câu trả lời sức khoẻ của họ.
  `user_id` cấp tuần tự nên không cần đoán.

Giờ cả hai chỉ đến từ token đã ký HMAC (`app/auth.py`). Body có gửi `user_id`
hay `tier` cũng bị bỏ qua — có test giữ đúng điều đó.

**Token cho SSE phải đi qua query param** `?token=...`: `EventSource` của trình
duyệt không gửi được header tuỳ ý. Token sẽ nằm trong access log của proxy, nên
loại token này cần TTL ngắn.

**Giả định cần xác nhận với team BE:** BE ký token bằng secret dùng chung. Nếu
BE đã phát JWT sẵn thì thay phần thân `verify_token`, phần còn lại chỉ phụ thuộc
vào `Principal`.

Fail-closed: `AUTH_REQUIRED=true` là mặc định, và bật auth mà quên đặt
`AUTH_SECRET` thì app **chết ngay lúc khởi động** chứ không âm thầm chấp nhận
mọi token.

### 2. Schema chưa từng chạy trên Postgres

Từ Phase 4 tới giờ test DB chỉ chạy trên SQLite. Nay `db/schema.sql` đã được
apply lên **PostgreSQL 16 thật** và toàn bộ test DB chạy lại trên đó:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://bcare@/bcare?host=/tmp pytest -q
```

`JSONB`, `TIMESTAMPTZ`, `BIGSERIAL`, unique index `(request_id, role)`, check
constraint `role`, FK `ON DELETE CASCADE` — đều đúng như thiết kế.

## Rollout dần

`app/services/rollout.py`: allowlist + phần trăm theo hash của `user_id`.

```
ROLLOUT_ENABLED=true
ROLLOUT_PERCENTAGE=0        # 0 = chỉ allowlist
ROLLOUT_ALLOWLIST=1,2,3     # nhóm nội bộ
```

Dùng **hash** chứ không phải `user_id % 100`: user_id cấp tuần tự nên chia lấy
dư sẽ gom user đăng ký cùng đợt vào cùng nhóm, mẫu rollout mất tính đại diện.
Kết quả phải **ổn định** giữa các request — không thì cùng một user lúc hỏi được
lúc không.

User ngoài nhóm nhận 403 với thông báo rõ ràng, và **không bị trừ quota**.

### Trình tự đề xuất

1. `ROLLOUT_PERCENTAGE=0`, `ROLLOUT_ALLOWLIST=<team nội bộ>` — chạy thật vài ngày.
2. Đọc `/metrics`: `cost_usd / openai_calls` là chi phí thật mỗi câu hỏi. So với
   ước tính đã review ở Phase 0 **trước khi** tăng phần trăm.
3. Theo dõi `input_blocked` / `output_blocked`: cao bất thường nghĩa là prompt
   hoặc filter đang chặn nhầm người dùng thật.
4. Tăng dần 1% → 5% → 25% → 100%, mỗi bậc để đủ lâu để thấy số liệu.
5. Có chuyện thì `ROLLOUT_ENABLED=false` — tắt ngay, không cần deploy.

## Đo chi phí

Mỗi lần gọi OpenAI đều ghi lại token usage (`app/services/cost.py`), đính vào
`ChatResponseMessage.usage` và lưu trong `chat_messages.meta`. `/metrics` cộng
dồn `openai_calls`, `tokens_total`, `cost_usd`.

⚠️ Đơn giá nằm trong config (`PRICE_INPUT_PER_1M`, `PRICE_OUTPUT_PER_1M`) với
giá trị mặc định cho `gpt-4o-mini`. **Phải đối chiếu lại với bảng giá hiện hành
của OpenAI** trước khi tin con số báo cáo — giá thay đổi theo thời gian và theo
model.

`/metrics` là số liệu của **một instance**, reset khi restart. Đủ cho giai đoạn
rollout nhóm nhỏ; mở rộng thì thay bằng Prometheus exporter, giữ nguyên tên chỉ số.

## Test

```bash
pytest -q                                                  # 164 test
TEST_DATABASE_URL=postgresql+asyncpg://... pytest -q        # cùng bộ đó trên Postgres
OPENAI_API_KEY=sk-... pytest -m live -q                     # 12 test gọi API thật
python -m scripts.loadtest --users 50 --questions 3         # cần Kafka/Redis/app thật
```

### Kịch bản "mất mạng thật" (`tests/test_network_failure.py`)

Ghép API → consumer → bộ đệm replay → SSE, chỉ thay Kafka và OpenAI bằng bản giả:

- Gửi câu hỏi rồi rớt mạng ngay, consumer vẫn xử lý xong lúc client offline.
- Client online lại, gửi lại y hệt: nhận được câu trả lời cũ (`status: done`).
- **Không lặp**: đúng 1 lần gọi OpenAI, 1 lần enqueue, 1 lượt quota.
- **Không mất**: mở lại SSE vẫn nhận được câu trả lời đã phát lúc offline.
- Retry 8 lần đồng thời cùng `request_id` → đúng 1 cái được chấp nhận.
- 30 user gửi đồng thời → không ai bị trừ nhầm quota của người khác.

### Load test (`scripts/loadtest.py`)

Chỉ chạy được với hạ tầng thật, và là thứ **duy nhất** kiểm được:

1. **Thứ tự theo user** với Kafka thật — sai nghĩa là partition key hỏng.
2. **Quota không race** khi gửi đồng thời thật.
3. **Độ trễ** p50/p95/p99 để so với SLA đã chốt ở Phase 0.

## Còn nợ trước khi lên production

Những thứ này **chưa từng được chạy**, không phải "đã chạy và ổn":

- `scripts/loadtest.py` chưa chạy lần nào (môi trường này không có Kafka).
- `pytest -m live` chưa chạy lần nào (không có `OPENAI_API_KEY`) — nghĩa là
  **chưa ai đo được model có chống được injection hay không**, và chưa ai đọc
  thử một câu trả lời thật để đánh giá chất lượng tiếng Việt.
- Chưa chạy nhiều instance cùng lúc để kiểm fanout SSE.
- Chưa kiểm qua nginx/LB thật (buffering, timeout kết nối idle).
- `gpt-4o-mini` chưa được benchmark chất lượng tiếng Việt so với model lớn hơn.

Và các món còn thiếu về mặt sản phẩm:

- Chưa có cách xoá/ẩn lịch sử theo yêu cầu người dùng — cần cho dữ liệu sức khoẻ.
- Chưa có công cụ replay message từ dead-letter về lại `chat_requests`.
- Chưa có alert (mới có counter): tỉ lệ vào DLQ, tỉ lệ 429, chi phí vượt ngưỡng.
- `/metrics` chưa được bảo vệ — không nên để lộ ra ngoài.
