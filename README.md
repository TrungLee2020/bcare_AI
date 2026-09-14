# bcare_AI

Lớp AI-answering cho bCare: nhận câu hỏi sức khoẻ / bảo hiểm qua Kafka, gọi
OpenAI trả lời, đẩy kết quả về client qua SSE. Có quota theo ngày, chống prompt
injection, và lưu lịch sử để hiểu ngữ cảnh hội thoại.

Thiết kế tổng thể: [`docs/plan.md`](docs/plan.md).

## Luồng chính

```
FE --POST /chat/ask--> [auth] -> [rollout] -> [idempotency] -> [quota] -> Kafka(chat_requests)
                                                                             |
                                                            consumer (1 user = 1 partition)
                                                                             |
                                    [lọc injection] -> [ngữ cảnh] -> OpenAI -> [validate output]
                                                                             |
                                              Kafka(chat_responses) -> SSE -> FE
```

## Chạy local

```bash
pip install -r requirements-dev.txt
cp .env.example .env          # đặt OPENAI_API_KEY và AUTH_SECRET
python -m scripts.create_topics
psql "$DATABASE_URL" -f db/schema.sql
uvicorn app.main:app --reload
```

## Test

```bash
pytest -q                                            # 164 test, không cần hạ tầng
TEST_DATABASE_URL=postgresql+asyncpg://... pytest -q  # cùng bộ đó trên Postgres thật
OPENAI_API_KEY=sk-... pytest -m live -q              # 12 test gọi OpenAI thật
python -m scripts.loadtest --users 50 --questions 3  # cần Kafka/Redis/app đang chạy
```

## Tài liệu theo phase

| Phase | Nội dung | Tài liệu |
|---|---|---|
| 1 | Kafka pipeline, thứ tự theo partition | [phase1](docs/phase1-verification.md) |
| 2 | Quota & idempotency | [phase2](docs/phase2-quota-idempotency.md) |
| 3 | Prompt có version, lọc injection, structured outputs | [phase3](docs/phase3-prompt-openai.md) |
| 4 | Ngữ cảnh hội thoại & tóm tắt phiên | [phase4](docs/phase4-context-history.md) |
| 5 | SSE, retry/backoff, dead-letter | [phase5](docs/phase5-sse-resilience.md) |
| 6 | Xác thực, rollout dần, đo chi phí | [phase6](docs/phase6-test-rollout.md) |

## Trước khi lên production

Xem mục "Còn nợ" ở [phase6](docs/phase6-test-rollout.md). Tóm tắt những thứ
**chưa từng chạy lần nào**: load test với Kafka thật, test injection với model
thật (`pytest -m live`), fanout SSE nhiều instance, và kiểm qua nginx/LB thật.
