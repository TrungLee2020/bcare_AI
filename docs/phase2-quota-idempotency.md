# Phase 2 — Quota & Idempotency

Endpoint chính thức cho FE: `POST /chat/ask`. `/test/enqueue` của Phase 1 chỉ
còn dùng để verify pipeline ở local (bypass quota, mặc định tắt).

## Luồng xử lý

```
POST /chat/ask
  1. idempotency.claim(request_id)     -- SET NX, atomic
       |-- đã tồn tại -> 200 duplicate/done (KHÔNG trừ quota, KHÔNG enqueue)
  2. quota.consume(user_id, tier)      -- Lua script, atomic
       |-- hết lượt -> release claim -> 429
  3. publish vào Kafka
       |-- lỗi -> refund quota + release claim -> 503
  4. 202 accepted + quota_remaining
```

**Vì sao idempotency đứng TRƯỚC quota:** nếu check quota trước, mỗi lần client
retry (mạng chập chờn, cùng `request_id`) đều trừ thêm 1 lượt — user free mất
sạch quota chỉ vì mạng yếu chứ không phải vì hỏi nhiều.

**`request_id` nên do client sinh.** Server chỉ dedup được khi client gửi lại
ĐÚNG `request_id` cũ. Nếu client không gửi, server tự sinh và mỗi lần retry là
1 câu hỏi mới (tốn quota) — điều này cần nói rõ với team FE.

## Quota

- Key: `quota:{user_id}:{YYYY-MM-DD}`, TTL tới nửa đêm **giờ VN** (không phải
  UTC — nếu dùng UTC thì quota reset lúc 7h sáng, user sẽ thắc mắc).
- Check + increment nằm trong 1 script Lua. Tách thành `GET` rồi `INCR` ở phía
  Python sẽ có race: 2 request đồng thời cùng đọc `used=1` và cùng đi qua.
  Test `test_concurrent_khong_vuot_limit` bắn 20 request song song và assert
  đúng 2 cái lọt (đây chính là bài test Phase 6 yêu cầu).
- `refund` không dùng `DECR` trần: nếu key đã hết hạn (sang ngày mới), `DECR`
  tạo key mới bằng -1 và user hôm sau được dư 1 lượt.

## Idempotency — 3 key khác nhau, đừng gộp

| Key | Tầng | Vai trò |
|---|---|---|
| `dedup:{request_id}` | API | giữ chỗ, chặn retry của client |
| `resp:{request_id}` | API/consumer | cache câu trả lời để trả lại ngay cho retry |
| `processed:{request_id}` | Consumer | chặn Kafka giao lại message sau khi consumer crash |

`dedup:` và `processed:` phải tách riêng: nếu dùng chung 1 key thì message vừa
được API enqueue sẽ bị chính consumer coi là "đã xử lý" và bỏ qua ngay.

Consumer commit offset SAU khi xử lý (`enable_auto_commit=False`), nên Kafka có
thể giao lại đúng message đó nếu consumer chết giữa chừng. `processed:` là thứ
chặn việc gọi OpenAI 2 lần cho cùng 1 câu hỏi ở Phase 3.

## Chạy test

```bash
pip install -r requirements-dev.txt
pytest -q
```

Test dùng `fakeredis` nên không cần Redis thật. Các case đang cover:
- free hết lượt sau 2 câu, premium sau 5 câu, quota tách biệt giữa user
- 20 request đồng thời của cùng 1 user free -> đúng 2 cái lọt
- gửi trùng `request_id` 5 lần -> 1 lần enqueue, 1 lượt quota
- 10 request trùng gửi song song -> đúng 1 cái `accepted`
- hết quota vẫn nhả `request_id` (hôm sau gửi lại cùng id vẫn hỏi được)
- enqueue lỗi -> hoàn quota, cho retry

## Còn nợ ở phase sau

- Câu trả lời thật (`resp:{request_id}`) chỉ được ghi từ Phase 3; hiện tại
  `status: "done"` chỉ xảy ra nếu có thứ khác ghi vào cache đó.
- Quota hiện trừ ở tầng API. Nếu sau này có đường enqueue khác (batch job,
  admin tool) thì phải trừ quota ở đó luôn, hoặc chuyển việc trừ xuống consumer.
