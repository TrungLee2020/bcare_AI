# API cho app mobile (`/v1`)

Phía service của hợp đồng trong PatronyApp `docs/ai-chat-api.md`. Code: [`app/api/v1.py`](../app/api/v1.py).

**Base URL:** `https://chat.but.care` (KAN-84), tức `AI_CHAT_URL=https://chat.but.care`.

## Trạng thái triển khai (cập nhật 2026-10-10)

| Hạng mục | Trạng thái |
|---|---|
| Code `/v1/chat`, `/v1/report/monthly` | Xong, đã lên server (nhánh `feat/v1-app-api`) |
| DNS `chat.but.care` → `14.225.29.169` | **Đang chờ** thêm bản ghi A ở nhà đăng ký (P.A Việt Nam) |
| Chứng chỉ HTTPS `chat.but.care` | Cấp ngay khi DNS có hiệu lực |
| Bảng `user_consents` trên Supabase | **Đang chờ** phía App deploy migration `20261010010000_ai_chat_consent.sql` |

Trước khi DNS xong, `chat.but.care` chưa phân giải được, app sẽ gặp lỗi mạng. App cứ build với `AI_CHAT_URL=https://chat.but.care`, không cần đổi gì khi domain lên. Có thể kiểm tra bằng `curl https://chat.but.care/health`, ra `{"status":"ok",...}` là sẵn sàng.

Phía App cần làm:
- Deploy migration `user_consents` lên Supabase. Chưa có bảng thì mọi request trả `503 unavailable`.
- Set `app_metadata.tier = "premium"` cho user premium, làm ở server bằng service role. Không set thì tính là free.
- Phân biệt lỗi theo `error`, không chỉ theo HTTP status: **chỉ 401 là hết phiên**, 403 không đăng xuất (xem mục Lỗi).
- Gửi `Idempotency-Key` khi thử lại (khuyến nghị).
- Gửi `SUPABASE_SERVICE_ROLE_KEY` cho backend qua kênh riêng (xem mục dưới).

## Cấu hình Supabase phía backend

Backend đặt các giá trị này **một lần** trong `.env` trên server. App **không** gửi chúng trong request.

| Biến | Giá trị | Ai cung cấp | Mức nhạy cảm |
|---|---|---|---|
| `SUPABASE_URL` | `https://zkqoaxfueiojwtvvoaov.supabase.co` | Có sẵn từ khi tạo project Supabase (project ref trong đặc tả của App). App cũng dùng đúng URL này. | Công khai |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API → `service_role` | **Team App** (người có quyền vào project) | **Bí mật — quyền cao nhất** |
| `SUPABASE_JWT_SECRET` | Supabase → Project Settings → API → JWT Secret | Team App, **chỉ khi** project còn ký token bằng JWT secret kiểu cũ (HS256). Project dùng signing key mới thì để trống. | Bí mật |

Backend dùng `SUPABASE_URL` để:
1. tải khoá công khai `…/auth/v1/.well-known/jwks.json` và verify access token app gửi lên;
2. kiểm token đúng do project này phát (`iss`);
3. đọc bảng `user_consents` qua `…/rest/v1/`.

`SUPABASE_SERVICE_ROLE_KEY` đọc/ghi được toàn bộ database và bỏ qua RLS:
- Gửi qua kênh riêng (tin nhắn riêng, password manager). Không dán vào group chat, Jira, và không commit lên git.
- Chỉ nằm trong `.env` trên server, **không bao giờ** nhúng vào app mobile.
- Lộ key thì xoay lại ngay trên Supabase rồi gửi key mới.

Nếu team App không muốn đưa service role key: tạo một hàm RPC (`security definer`) chỉ trả `ai_chat_version`, `ai_share_profile` của một user. Backend sẽ gọi hàm đó bằng key quyền hẹp. Cách này cần sửa thêm [`app/services/consent.py`](../app/services/consent.py).

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
4. DNS: bản ghi `A` `chat` → IP server.
5. Chứng chỉ, xin TRƯỚC khi copy config nginx mới (config mới trỏ tới `/etc/letsencrypt/live/chat.but.care/`, chưa có thì `nginx -t` lỗi): `certbot certonly --webroot -w /var/www/html -d chat.but.care`. Lỗi xác minh thì dùng `systemctl stop nginx && certbot certonly --standalone -d chat.but.care; systemctl start nginx`.
6. nginx: copy lại `deploy/nginx/bcare-ai.conf`, `nginx -t && systemctl reload nginx`.
