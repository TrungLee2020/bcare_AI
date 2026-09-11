-- Schema Postgres cho lịch sử chat (Phase 1 - Data Layer).
-- Phase 2 chưa ghi vào các bảng này; consumer sẽ ghi từ Phase 3 khi đã có câu
-- trả lời thật, và Phase 4 dùng `chat_sessions.summary` để nạp ngữ cảnh.

CREATE TABLE IF NOT EXISTS chat_sessions (
    id          UUID PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    -- Tóm tắt phiên, sinh định kỳ ở Phase 4 để không phải nhồi full history
    -- vào prompt (vừa tốn token vừa giảm chất lượng trả lời).
    summary     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_created
    ON chat_sessions (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  UUID        NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL,
    -- request_id của message gốc từ Kafka. UNIQUE để idempotency không chỉ dựa
    -- vào Redis: nếu Redis mất dữ liệu (restart, evict), ràng buộc này vẫn chặn
    -- việc ghi trùng 1 câu hỏi vào lịch sử.
    request_id  UUID        NOT NULL UNIQUE,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    -- Metadata của lần gọi OpenAI (model, token usage, out_of_scope...) - Phase 3
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
    ON chat_messages (session_id, created_at);
