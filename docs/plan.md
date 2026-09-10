# Mục tiêu

Xây dựng lớp AI-answering service tích hợp vào BE hiện có (Kafka + SSE), dùng OpenAI để trả lời câu hỏi sức khỏe/bảo hiểm cho user, có giới hạn quota/ngày, chống injection, và lưu lịch sử để phân tích ngữ cảnh dài hạn.

## Phase 0 — Chốt thiết kế & ràng buộc (trước khi code)
Chốt quota theo tier: ví dụ free = 2 câu/ngày, premium = 5 câu/ngày (map với model affiliate/premium đã có).
Chốt phạm vi nội dung được phép trả lời (sức khỏe chung, giải thích điều khoản bảo hiểm...) và danh sách chủ đề bị chặn cứng (chẩn đoán xác định, kê đơn, tư vấn pháp lý...).
Chốt SLA độ trễ trả lời (vì qua Kafka + OpenAI có thể mất vài giây) để set timeout hợp lý cho SSE.
Chốt lưu trữ: Postgres cho structured history, Redis cho quota + dedup, có cần vector DB cho semantic search lịch sử cũ không (nếu muốn "phân tích ngữ cảnh về sau" ở mức sâu hơn full-text).

## Phase 1 — Hạ tầng nền (Infra & Data Layer)
Schema Postgres: chat_sessions, chat_messages (đã phác ở trên).
Redis: quota counter (quota:{user_id}:{date}), dedup set cho request_id đã xử lý (TTL 48h), cache response gần nhất theo request_id.
Kafka topics: chat_requests (partition theo user_id), chat_responses.
Consumer group skeleton (FastAPI + aiokafka hoặc confluent-kafka-python), chưa gọi OpenAI, chỉ log + ack để verify pipeline chạy đúng thứ tự, đúng partition.

## Phase 2 — Quota & Idempotency
Middleware/service check quota trước khi enqueue vào Kafka (chặn sớm, đỡ tốn tài nguyên) — trả lỗi rõ ràng ("hết lượt hỏi hôm nay") qua API/SSE.
Idempotency check bằng request_id: nếu đã xử lý → trả lại response cũ ngay, không enqueue lại.
Test case: gửi trùng request_id nhiều lần liên tiếp (giả lập mất mạng/retry), verify chỉ tốn 1 quota và 1 lần gọi OpenAI.

## Phase 3 — Prompt Engineering & OpenAI Integration
Viết system prompt chuẩn (bản đã gửi ở trên) — tách riêng thành template file, versioned (để sau này chỉnh không cần deploy lại code).
Layer input filtering độc lập trước khi gửi OpenAI (regex/keyword chặn injection cơ bản).
Dùng Structured Outputs (response_format: json_schema) để ép format trả lời cố định, dễ validate + hiển thị FE.
Layer output validation: check field out_of_scope, độ dài, có lọt system prompt ra ngoài không (so khớp substring cơ bản).
Unit test injection: tập hợp ~20-30 câu prompt injection mẫu (tiếng Việt + tiếng Anh), verify model không bị chi phối.

## Phase 4 — Context & History
Khi có câu hỏi mới: load N tin nhắn gần nhất + summary phiên (nếu có) làm context.
Job định kỳ (hoặc trigger sau mỗi K tin nhắn) để tóm tắt session cũ, lưu vào chat_sessions.summary, tránh nhồi full history vào prompt.
(Optional, nếu cần phân tích xu hướng sức khỏe user theo thời gian dài) — cân nhắc thêm bảng user_health_insights được extract định kỳ từ history, phục vụ báo cáo/dashboard sau này.

## Phase 5 — SSE & Resilience
SSE connection map theo user_id, hỗ trợ reconnect kèm last_received_request_id để replay response bị miss.
Xử lý timeout: nếu OpenAI trả chậm quá SLA, có cơ chế gửi "đang xử lý" qua SSE tránh client tưởng mất kết nối.
Retry policy cho Kafka consumer khi OpenAI lỗi (rate limit, timeout): backoff + dead-letter topic cho case lỗi liên tục.

## Phase 6 — Test & Rollout
Load test: giả lập nhiều user gửi đồng thời, verify partition theo user_id giữ đúng thứ tự, quota không bị race condition (test concurrent increment).
Test mất mạng thực tế: ngắt kết nối client giữa chừng, gửi lại, verify không mất/không lặp câu trả lời.
Rollout dần: bật cho 1 nhóm user nhỏ trước, theo dõi chi phí OpenAI thực tế so với ước tính đã review trước đó.