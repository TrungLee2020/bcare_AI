-- Index cho danh sách hội thoại của app (GET /v1/conversations gom message theo user).
-- Chạy MỘT lần trên DB đang có dữ liệu (DB mới thì db/schema.sql đã có):
--   psql "$DATABASE_URL" -f db/migrations/20261102_chat_messages_user_index.sql
-- CONCURRENTLY để không khoá ghi lịch sử trong lúc tạo; không chạy được trong
-- transaction nên file này không có BEGIN/COMMIT.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_messages_user_id ON chat_messages (user_id, id);
