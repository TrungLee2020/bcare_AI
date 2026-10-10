# API cho app mobile (`/v1`)

Phía service của hợp đồng trong PatronyApp `docs/ai-chat-api.md`. Code: [`app/api/v1.py`](../app/api/v1.py).

**Base URL:** `https://chat.but.care` (KAN-84), tức `AI_CHAT_URL=https://chat.but.care`. `https://ai.but.care` trỏ vào cùng service, dùng chung một chứng chỉ Let's Encrypt.

## Xác thực

`Authorization: Bearer <access token Supabase>`, project `zkqoaxfueiojwtvvoaov`.

- Service tự verify: chữ ký (JWKS ES256/RS256, hoặc HS256 nếu project còn dùng JWT secret cũ), `exp`, `aud=authenticated`, `iss=https://zkqoaxfueiojwtvvoaov.supabase.co/auth/v1`, `role=authenticated`. Token anon key bị từ chối.
- `sub` là user id.
- Gói lấy từ `app_metadata.tier` (`"premium"`, còn lại là free). Chỉ service role ghi được `app_metadata`. `user_metadata` thì user tự sửa được, nên không đọc gói từ đó.
- Hạn mức: free 2 câu/ngày, premium 5 câu/ngày, reset 0h giờ VN.

## Lỗi

Mọi lỗi do service trả về đều có dạng `{"error": "<mã>", "message": "<câu hiển thị được>", ...}`:

| HTTP | `error` | App nên làm |
|---|---|---|
| 401 | `unauthorized` | Token hỏng/hết hạn → refresh session hoặc đăng nhập lại. **Chỉ 401 là hết phiên.** |
| 403 | `consent_required` | Chưa đồng ý Trợ lý AI → hiện màn đồng ý. Không đăng xuất. |
| 403 | `not_enabled` | Tài khoản chưa nằm trong nhóm được mở → ẩn hoặc báo "sắp có". Không đăng xuất. |
| 422 | `invalid_request` / `message_too_long` | Body sai hợp đồng / câu hỏi > 2000 ký tự (kèm `max_chars`). |
| 413 | `payload_too_large` | Số liệu báo cáo quá lớn. |
| 429 | `quota_exceeded` | Hết lượt hôm nay. Kèm `limit`, `retry_after` (giây tới 0h) và header `Retry-After`. |
| 429 | `rate_limited` | Gọi dồn dập / quá số lần tạo báo cáo trong ngày. 429 từ nginx (body HTML, không phải JSON) cũng hiểu là loại này. |
| 503 | `unavailable` | Lỗi phía service → cho thử lại. |

## `POST /v1/chat`

```json
{
  "conversation_id": "uuid do service cấp, bỏ trống ở câu đầu",
  "messages": [{"role": "user", "content": "..."}],
  "locale": "vi",
  "client_safety": "none",
  "health_context": {"...": "..."}
}
```

- **`messages`**: tối đa 48 message (24 lượt). Lượt cuối phải là `role: "user"`, dài 1–2000 ký tự. **Service chỉ dùng lượt user cuối cùng.** Ngữ cảnh hội thoại do service tự lưu theo `conversation_id`, vì các lượt `assistant` do client gửi lên có thể bị sửa để chèn chỉ dẫn vào model. App cứ gửi đủ như đặc tả.
- **`conversation_id`**: lấy từ `done` của lượt trước. Thiếu, hoặc không phải id do service cấp, thì mở hội thoại mới.
- **`client_safety`** `crisis` / `emergency`: service **không gọi model và không trừ lượt**, trả câu cố định: `emergency` khuyên gọi 115, `crisis` hướng về 115 và màn hỗ trợ tâm lý của app. Cờ `should_see_doctor` = true.
- **`health_context`**: chỉ dùng khi `user_consents.ai_share_profile = true`, nếu không thì bị bỏ qua. Service phẳng hoá thành các dòng `khoá: giá trị`, giới hạn 2000 ký tự, và chỉ đưa vào prompt của đúng lượt đó dưới dạng dữ liệu tham khảo. **Không ghi vào lịch sử chat.** Nếu có dấu hiệu prompt injection thì bỏ hồ sơ nhưng vẫn trả lời.
- **`locale`**: hiện chỉ trả lời tiếng Việt.
- **`Idempotency-Key`** (header, tuỳ chọn): giữ nguyên khi thử lại vì rớt mạng thì lần thử lại không trừ thêm lượt và nhận đúng câu trả lời của lần đầu.

### Trả về: SSE (mặc định)

Trả SSE trừ khi `Accept` chỉ có `application/json`.

```
: ok

: ping

data: {"type":"delta","text":"Đau đầu kéo dài 3 ngày "}

data: {"type":"delta","text":"có nhiều nguyên nhân..."}

data: {"type":"done","conversation_id":"…","should_see_doctor":false,"follow_up_questions":["…"],"quota_remaining":1}
```

- Dòng bắt đầu bằng `:` là comment giữ kết nối, gửi mỗi 15 giây trong lúc chờ. App bỏ qua.
- Câu trả lời được sinh xong rồi mới chia thành nhiều `delta` (khoảng 40 ký tự/phần). Nối các `text` lại ra đúng nguyên văn.
- `done` có thêm `should_see_doctor`, `follow_up_questions`, `quota_remaining`. App chưa dùng thì bỏ qua.
- Lỗi sau khi stream đã mở (quá 120 giây, lỗi model): `data: {"type":"error","code":"unavailable","message":"…"}` rồi đóng. Lượt đó được hoàn.
- App bỏ qua mọi `type` lạ.

### Trả về: JSON (`Accept: application/json`)

```json
{"reply":"…","conversation_id":"…","should_see_doctor":false,"follow_up_questions":[],"quota_remaining":1}
```

## `POST /v1/report/monthly`

Body là object số liệu tổng hợp 2 tháng do app tự tính, tối đa 6000 ký tự khi serialize. Service không ép schema chi tiết: nhận nguyên khối, đưa vào prompt như dữ liệu.

Trả về `{"summary": "3–5 câu…"}`.

- Cùng số liệu thì trả bản đã cache (7 ngày), không gọi lại model.
- Không trừ lượt chat. Mỗi user tối đa 5 lần tạo mới mỗi ngày (`rate_limited`).
- Đoạn tóm tắt không chẩn đoán, không bàn đổi/ngừng thuốc, không nêu liều. Bản nào vi phạm thì bị chặn, trả `503 unavailable`, và app ẩn ô "Bụt tóm tắt".
- Cần `ai_chat_version` (đồng ý AI) như `/v1/chat`.

## Đồng ý (consent)

Đọc `user_consents` (cột `user_id`, `ai_chat_version`, `ai_share_profile`) qua PostgREST bằng service role key, cache 60 giây.

- `ai_chat_version` null → `403 consent_required`.
- Đặt `CONSENT_REQUIRED_VERSION` thì bắt buộc đúng version đó.
- Không đọc được bảng (chưa migrate, sai key, Supabase lỗi) → `503`. Không bao giờ coi là đã đồng ý.

## Deploy

1. `psql "$DATABASE_URL" -f db/migrations/20261010_user_id_supabase_uuid.sql` (`user_id` BIGINT → VARCHAR(64)).
2. `.env`: đặt `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (và `SUPABASE_JWT_SECRET` nếu project dùng HS256). `ROLLOUT_ALLOWLIST` giờ là danh sách UUID.
3. Trước khi migration `20261010010000_ai_chat_consent.sql` lên Supabase: `CONSENT_REQUIRED=false` (chỉ ở môi trường test; `APP_ENV=production` từ chối khởi động với giá trị này).
4. DNS: bản ghi `A` `chat` → cùng IP với `ai.but.care`.
5. nginx: copy lại `deploy/nginx/bcare-ai.conf` (thêm `chat.but.care`, `location /v1/`), `nginx -t && systemctl reload nginx`.
6. Mở rộng chứng chỉ: `certbot certonly --webroot -w /var/www/html --cert-name ai.but.care --expand -d ai.but.care -d chat.but.care`, rồi `systemctl reload nginx`.
