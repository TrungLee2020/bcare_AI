-- user_id: BIGINT -> VARCHAR(64), để chứa `sub` (UUID) của access token Supabase.
-- Chạy MỘT lần trên DB đang có dữ liệu (DB mới thì db/schema.sql đã đúng):
--   psql "$DATABASE_URL" -f db/migrations/20261010_user_id_supabase_uuid.sql
-- Lịch sử cũ (user_id số, từ token HMAC thử nghiệm) giữ nguyên dưới dạng chuỗi;
-- không trùng được với UUID nên không user mới nào thấy lịch sử cũ.
BEGIN;
ALTER TABLE chat_sessions ALTER COLUMN user_id TYPE VARCHAR(64) USING user_id::text;
ALTER TABLE chat_messages ALTER COLUMN user_id TYPE VARCHAR(64) USING user_id::text;
COMMIT;
