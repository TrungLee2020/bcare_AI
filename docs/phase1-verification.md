# Verify Phase 1 (thứ tự message + partition)

Mục tiêu Phase 1 chỉ là verify pipeline chạy đúng, chưa gọi OpenAI.

## Chạy local

```bash
pip install -r requirements.txt
cp .env.example .env   # sửa KAFKA_BOOTSTRAP_SERVERS / REDIS_URL nếu không chạy ở localhost
python -m scripts.create_topics        # tạo chat_requests + chat_responses với 6 partition
ENABLE_TEST_ENDPOINTS=true uvicorn app.main:app --reload
```

`/test/enqueue` bypass quota nên mặc định TẮT — phải set `ENABLE_TEST_ENDPOINTS=true`
mới dùng được cho các bài test dưới đây (endpoint thật cho FE là `POST /chat/ask`,
xem `docs/phase2-quota-idempotency.md`).

Topic `chat_requests` cần có nhiều hơn 1 partition (ví dụ 6, xem
`KAFKA_NUM_PARTITIONS`; `scripts/create_topics.py` lo việc này) — nếu chỉ có 1 partition thì không test được đúng
việc "khác user vẫn có thể chạy song song, cùng user luôn đúng thứ tự".

## Test thứ tự message trong cùng 1 user_id

Gửi liên tiếp nhiều request cho CÙNG `user_id`, xem log consumer:

```bash
for i in 1 2 3 4 5; do
  curl -s -X POST localhost:8000/test/enqueue \
    -H 'Content-Type: application/json' \
    -d "{\"user_id\": 42, \"tier\": \"free\", \"content\": \"câu hỏi số $i\"}"
done
```

Kỳ vọng trong log consumer: cùng 1 `partition`, `offset` tăng dần đúng thứ tự
1→5. Nếu thấy partition khác nhau cho cùng user_id, nghĩa là key partitioning
đang sai (kiểm tra lại `producer.py`, key phải là `user_id` không đổi).

## Test rebalance khi có nhiều consumer instance

Chạy 2 instance của app (2 process `uvicorn` khác port, cùng `KAFKA_CONSUMER_GROUP`),
gửi request cho nhiều `user_id` khác nhau, verify:
- Message của các user khác nhau được chia đều cho 2 instance xử lý (tuỳ phân
  bổ partition, không phải chia đều tuyệt đối 50/50).
- Message của CÙNG 1 user_id luôn được xử lý bởi CÙNG 1 instance tại 1 thời
  điểm (vì cùng partition, và 1 partition chỉ gán cho 1 consumer trong group).

## Test crash-safety (offset chỉ commit sau khi xử lý)

Kill consumer instance (Ctrl+C) ngay sau khi thấy log "Consumed..." nhưng
trước dòng commit tiếp theo, start lại — message đó phải được xử lý (log) lại
lần nữa, không bị mất. Đây là hành vi đúng theo thiết kế
`enable_auto_commit=False`; ở Phase 2 sẽ cần xử lý duplicate này bằng
idempotency check (Redis `dedup:{request_id}`), không phải ở tầng Kafka.
