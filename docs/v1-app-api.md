# API cho app mobile (`/v1`)

Phía service của hợp đồng trong PatronyApp `docs/ai-chat-api.md`. Code: [`app/api/v1.py`](../app/api/v1.py).

**Base URL:** `https://ai.but.care` (KAN-84, chốt 2026-10-14), tức `AI_CHAT_URL=https://ai.but.care`.

## Trạng thái triển khai (cập nhật 2026-10-14)

| Hạng mục | Trạng thái |
|---|---|
| Code `/v1/chat`, `/v1/report/monthly` | Xong, đã lên server (nhánh `feat/v1-app-api`) |
| Lịch sử chat `GET /v1/conversations`, `…/{id}/messages` | Đã deploy (2026-11-02) |
| Xoá lịch sử `DELETE /v1/conversations[/{id}]` | Code xong (2026-11-03), chưa deploy. Cột mới do service `migrate` tự thêm. |
| DNS `ai.but.care` → `14.225.29.169` | Xong |
| Chứng chỉ HTTPS `ai.but.care` | Xong, tự gia hạn |
| Route `/v1/` trong nginx | Xong (2026-10-14) |
| Bảng `user_consents` trên Supabase | **Đang chờ** phía App deploy migration `20261010010000_ai_chat_consent.sql` |

Kiểm tra: `curl https://ai.but.care/health` ra `{"status":"ok",...}`; `curl -X POST https://ai.but.care/v1/chat` không ra 404 là route `/v1/` đã lên.

Phía App cần làm:
- Deploy migration `user_consents` lên Supabase. Chưa có bảng thì mọi request trả `503 unavailable`.
- Set `app_metadata.tier = "premium"` cho user premium, làm ở phía App bằng service role (key này không đưa cho backend AI). Không set thì tính là free.
- Phân biệt lỗi theo `error`, không chỉ theo HTTP status: **chỉ 401 là hết phiên**, 403 không đăng xuất (xem mục Lỗi).
- Gửi `Idempotency-Key` khi thử lại (khuyến nghị).
- Gửi anon key cho backend (xem mục dưới). Backend **không** cần service role key.

## Cấu hình Supabase phía backend

Backend đặt các giá trị này **một lần** trong `.env` trên server. App **không** gửi chúng trong request.

| Biến | Giá trị | Ai cung cấp | Mức nhạy cảm |
|---|---|---|---|
| `SUPABASE_URL` | `https://zkqoaxfueiojwtvvoaov.supabase.co` | Có sẵn từ khi tạo project Supabase (project ref trong đặc tả của App). App cũng dùng đúng URL này. | Công khai |
| `SUPABASE_ANON_KEY` | Supabase → Project Settings → API Keys → `anon` / publishable | Team App | Công khai (app cũng nhúng) |
| `SUPABASE_JWT_SECRET` | Supabase → Project Settings → API → JWT Secret | Team App, **chỉ khi** project còn ký token bằng JWT secret kiểu cũ (HS256). Project dùng signing key mới thì để trống. | Bí mật |

Backend dùng `SUPABASE_URL` để:
1. tải khoá công khai `…/auth/v1/.well-known/jwks.json` và verify access token app gửi lên;
2. kiểm token đúng do project này phát (`iss`);
3. đọc bảng `user_consents` qua `…/rest/v1/`.

Đọc `user_consents` bằng anon key + **access token của chính user** (`apikey: <anon>`, `Authorization: Bearer <token user>`). RLS cho user đọc dòng của mình, nên backend không giữ key nào vượt RLS. Đề xuất của team App, áp dụng 2026-10-19 (trước đó dùng service role key).

## Xác thực

`Authorization: Bearer <access token Supabase>`, project `zkqoaxfueiojwtvvoaov`.

- Service tự verify: chữ ký (JWKS ES256/RS256, hoặc HS256 nếu project còn dùng JWT secret cũ), `exp`, `aud=authenticated`, `iss=https://zkqoaxfueiojwtvvoaov.supabase.co/auth/v1`, `role=authenticated`. Token anon key bị từ chối.
- `sub` là user id.
- Gói lấy từ `app_metadata.tier` (`"premium"`, còn lại là free). Chỉ service role ghi được `app_metadata`. `user_metadata` thì user tự sửa được, nên không đọc gói từ đó.
- Hạn mức: free 2 câu/ngày, premium 5 câu/ngày, reset 0h giờ VN.
- User có UUID trong `QUOTA_UNLIMITED_USERS` (admin, tài khoản test; đặt ở `.env` server) chat không giới hạn và luôn được bật dù ngoài rollout. `quota_remaining` của họ luôn bằng hạn mức của gói.

## Lỗi

Mọi lỗi do service trả về đều có dạng `{"error": "<mã>", "message": "<câu hiển thị được>", ...}`:

| HTTP | `error` | App nên làm |
|---|---|---|
| 401 | `unauthorized` | Token hỏng/hết hạn → refresh session hoặc đăng nhập lại. **Chỉ 401 là hết phiên.** |
| 403 | `consent_required` | Chưa đồng ý Trợ lý AI → hiện màn đồng ý. Không đăng xuất. |
| 403 | `not_enabled` | Tài khoản chưa nằm trong nhóm được mở → ẩn hoặc báo "sắp có". Không đăng xuất. |
| 404 | `not_found` | Không có hội thoại này (sai id, hoặc của tài khoản khác) → bỏ khỏi danh sách trên app. |
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
- **Câu chào** (tin nhắn chỉ có lời chào: "hi", "hello", "chào bạn", "xin chào Bụt ạ", viết không dấu cũng được): service **không gọi model và không trừ lượt**, trả câu cố định "Chào bạn! Mình là Bụt, …" kèm 3 câu hỏi gợi ý. Chào kèm câu hỏi ("Chào bạn, tôi bị đau đầu") vẫn đi qua model như thường.
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

## Ảnh giấy tờ y tế (OCR)

Thiết kế đầy đủ: [docs/superpowers/specs/2026-11-06-chat-image-ocr-design.md](superpowers/specs/2026-11-06-chat-image-ocr-design.md).

App upload ảnh lên Supabase Storage (bucket `health-documents`, đường dẫn `<user_id>/<uuid>.jpg`) rồi gửi đường dẫn trong `POST /v1/chat`. Backend tải ảnh bằng token của user, phân loại, đọc chữ, trích xuất và lưu. Chỉ xử lý khi user đã đồng ý `ai_chat` **và** `ai_share_profile`. Tính năng bật bằng `OCR_ENABLED`.

**Rollout:** chỉ bật `OCR_ENABLED` khi **mọi** instance đã chạy bản này. Consumer bản cũ không biết `document_ids`: tin chỉ có ảnh sẽ bị coi là câu hỏi rỗng.

### `attachments` trong `POST /v1/chat`

```json
{
  "messages": [{"role": "user", "content": "Đơn thuốc này uống thế nào?"}],
  "attachments": [{"path": "<user_id>/<uuid>.jpg", "profile_id": null}]
}
```

- Tối đa 3 ảnh mỗi tin (`OCR_MAX_ATTACHMENTS`). `profile_id` là chuỗi app tự định nghĩa (thành viên gia đình), `null` = chủ tài khoản.
- Ảnh phải là JPEG, PNG hoặc WebP thuần, tối đa 10 MB.
- Tin chỉ có ảnh (`content` rỗng): **không gọi model chat, không trừ lượt chat**, API trả ngay câu cố định.
- Response JSON và event `done` (SSE) có thêm `documents`:

```json
{"reply":"…","conversation_id":"…","quota_remaining":1,"documents":[{"id":"uuid","status":"received"}]}
```

```
data: {"type":"done","conversation_id":"…","documents":[{"id":"uuid","status":"received"}],"quota_remaining":1,…}
```

Các ảnh của một tin được đọc song song, tổng thời gian tối đa `OCR_MESSAGE_BUDGET_SECONDS` (30 giây). Ảnh chưa đọc xong khi hết giờ chuyển thành `failed`; user gửi lại ảnh đó để đọc lại.

Nếu request có ảnh thất bại sau khi đã nhận ảnh (429 hết lượt, 403/422 ở ảnh sau, 503), các tài liệu đã nhận được đánh dấu `failed`. Gửi lại đúng ảnh đó thì được đọc lại.

### `GET /v1/documents?type=&limit=&cursor=`

Tài liệu chưa xoá của user, mới trước. `type`: `prescription`, `lab_result`, `medical_record`, `imaging_report`, `imaging_film`, `other_medical`, `not_medical`. `limit` mặc định 20, tối đa 50. `cursor` = `next_cursor` của trang trước, **app phải URL-encode** (có dấu `+` và `:`).

```json
{
  "documents": [
    {"id":"uuid","doc_type":"prescription","title":"Đơn thuốc – Bệnh viện Đa khoa – 03/11/2026","doc_date":"2026-11-03","status":"done","storage_path":"<user_id>/<uuid>.jpg","profile_id":null,"created_at":"2026-11-06T03:12:44+00:00"}
  ],
  "next_cursor": null
}
```

App tự lấy ảnh từ Storage bằng `storage_path`. **Luôn lấy ảnh qua `documents[].id` → `storage_path`**, không dùng đường dẫn app vừa upload: gửi lại một ảnh đã có (trùng nội dung), service giữ bản cũ và **xoá object vừa upload**, nên `storage_path` của tài liệu là đường dẫn của lần upload đầu.

### `GET /v1/documents/{id}`

Như một mục ở trên, thêm `ocr_text`, `extraction` (bản trích xuất mới nhất, theo từng loại, xem spec mục 5) và `uncertain_fields` (đường dẫn các trường model không chắc, app nên đánh dấu). Tài liệu của người khác hoặc đã xoá: `404 not_found`.

```json
{"id":"uuid","doc_type":"lab_result","status":"done","ocr_text":"…","extraction":{"doc_date":"2026-11-01","tests":[{"name":"Glucose","value":"7.8","unit":"mmol/L","reference_range":"3.9 - 6.4","flag_as_printed":"H"}],"uncertain_fields":[]},"uncertain_fields":[],"…":"…"}
```

### `DELETE /v1/documents/{id}`

Ẩn ngay và xoá ảnh trên Storage bằng token user, trả `204`. Bản ghi DB bị xoá hẳn sau `HISTORY_PURGE_AFTER_DAYS` ngày. Xoá hội thoại không xoá tài liệu. Tài liệu không có, đã xoá, hoặc của tài khoản khác: `404 not_found`.

### `DELETE /v1/documents`

Xoá **toàn bộ** giấy tờ của tài khoản: ẩn mọi tài liệu ngay và xoá từng ảnh trên Storage bằng token user (xoá ảnh lỗi thì vẫn trả `204`, service ghi log). Luôn trả `204` (kể cả khi không có gì để xoá), nên gọi lại an toàn.

**Xoá tài khoản:** app gọi `DELETE /v1/documents` **và** `DELETE /v1/conversations` **trước khi** xoá tài khoản Supabase (xoá tài khoản xong thì token không dùng được để xoá ảnh nữa). Gọi lỗi (`503`, mất mạng) thì chưa xoá tài khoản, cho user thử lại.

### Trạng thái (`status`)

| `status` | Ý nghĩa | Trừ hạn mức ảnh |
|---|---|---|
| `received` | Đã nhận, chưa đọc | không |
| `processing` | Đang đọc | không |
| `done` | Đã đọc và trích xuất. Phim chụp (`imaging_film`) cũng là `done` nhưng chỉ phân loại và lưu, không đọc: `ocr_text` / `extraction` là `null` | chỉ khi bước OCR thực sự chạy (phim chụp: không) |
| `skipped` | Không phải giấy tờ y tế, hoặc phân loại không đủ chắc chắn | không |
| `unreadable` | Ảnh mờ / không đọc được, nên chụp lại | không |
| `quota_exceeded` | Hết `OCR_MAX_PER_DAY` (3 ảnh/ngày) | không |
| `failed` | Lỗi model, hết `OCR_MESSAGE_BUDGET_SECONDS`, hoặc request thất bại sau khi nhận ảnh; gửi lại để đọc lại | không (hoàn nếu đã trừ) |

### Lỗi riêng cho ảnh

| HTTP | `error` | Khi nào |
|---|---|---|
| 403 | `not_enabled` | `OCR_ENABLED` tắt mà tin có `attachments` |
| 403 | `consent_required` kèm `"consent": "share_profile"` | Chưa đồng ý chia sẻ hồ sơ: App hiện popup chia sẻ |
| 403 | `forbidden` | `path` không thuộc `<user_id>` của token |
| 422 | `invalid_attachment` | Không tìm thấy ảnh, không phải JPEG/PNG/WebP thuần, quá 10 MB |
| 422 | `invalid_request` kèm `max_attachments` | Quá số ảnh mỗi tin |

### Việc của team App

1. Tạo bucket riêng tư `health-documents`; policy: user chỉ đọc / ghi / xoá object dưới `{auth.uid()}/`.
2. Trước khi upload, **re-encode mọi ảnh thành JPEG thuần**: chuyển HEIC sang JPEG, xoá EXIF, thu nhỏ cạnh dài tối đa 2048 px. JPEG chụp từ điện thoại mà Pillow nhận là "MPO" bị từ chối (`422 invalid_attachment`), nên không upload nguyên file camera.
3. Gửi `attachments` (`path`, `profile_id`) trong `POST /v1/chat`; xử lý `documents` trong response / `done`.
4. Màn hồ sơ: danh sách, chi tiết (dữ liệu trích xuất, đánh dấu `uncertain_fields`), xoá, hiển thị trạng thái (kể cả `failed` / `unreadable` với nút gửi lại). Hiển thị ảnh qua `documents[].id` → `storage_path`, không qua đường dẫn vừa upload.
5. Nội dung popup chia sẻ hồ sơ nói rõ có lưu ảnh và dữ liệu giấy tờ y tế.
6. Xoá tài khoản: gọi `DELETE /v1/documents` và `DELETE /v1/conversations` trước khi xoá tài khoản Supabase.

## Lịch sử chat

Đọc lại và xoá những gì service đã lưu theo `conversation_id`. Chỉ cần `Authorization` như trên; **không** kiểm đồng ý hay rollout (đây là dữ liệu của chính user) và không trừ lượt. Mỗi user chỉ thấy và xoá được hội thoại của mình.

**Thời hạn lưu:** lịch sử được lưu cho tới khi user xoá (từng hội thoại, hoặc toàn bộ khi xoá tài khoản). Xoá thì hội thoại biến mất ngay khỏi lịch sử và khỏi ngữ cảnh của Bụt; service xoá hẳn khỏi DB sau 14 ngày (`HISTORY_PURGE_AFTER_DAYS`). Luật BVDLCN 2025 / NĐ 356/2025 cho khoảng 20 ngày để thực hiện yêu cầu xoá.

**Những lượt không có trong lịch sử:** câu chào, lượt `client_safety` `crisis`/`emergency`, câu bị chặn (`blocked`) và lượt lỗi. Các lượt này không gọi model hoặc không có câu trả lời thật, nên service không lưu. Hội thoại chỉ có những lượt đó thì không xuất hiện trong danh sách.

Phân trang bằng `cursor`: lấy `next_cursor` của trang trước gửi lại; `next_cursor: null` là hết. `limit` quá trần thì bị kẹp, không báo lỗi. `cursor`/`limit` không phải số → `422 invalid_request`.

### `GET /v1/conversations?limit=20&cursor=…`

Danh sách hội thoại, hội thoại có tin nhắn mới nhất đứng trước. `limit` mặc định 20, tối đa 50.

```json
{
  "conversations": [
    {
      "conversation_id": "uuid",
      "title": "Câu hỏi đầu tiên, tối đa 80 ký tự…",
      "preview": "Tin nhắn cuối cùng (thường là câu trả lời), tối đa 160 ký tự…",
      "message_count": 6,
      "created_at": "2026-11-02T03:15:20.123456+00:00",
      "updated_at": "2026-11-02T03:20:41.654321+00:00"
    }
  ],
  "next_cursor": "1234"
}
```

`title` và `preview` đã gộp xuống dòng thành dấu cách và cắt kèm `…`. Thời gian là ISO 8601, UTC.

### `GET /v1/conversations/{conversation_id}/messages?limit=50&cursor=…`

Tin nhắn của một hội thoại. Trang đầu là các tin **mới nhất**, trả theo thứ tự cũ → mới (đúng thứ tự hiển thị). Kéo lên xem tin cũ hơn thì gửi `next_cursor`. `limit` mặc định 50, tối đa 100.

```json
{
  "conversation_id": "uuid",
  "messages": [
    {"id": "1201", "role": "user", "content": "Tôi bị đau đầu 3 ngày", "created_at": "…"},
    {"id": "1202", "role": "assistant", "content": "…", "created_at": "…",
     "should_see_doctor": false, "follow_up_questions": ["…"]}
  ],
  "next_cursor": "1201"
}
```

- `should_see_doctor`, `follow_up_questions` chỉ có ở tin `assistant`. Lượt lưu trước 2026-11-02 có `follow_up_questions: []`.
- Tin `user` gửi kèm ảnh có thêm `documents`: danh sách id tài liệu (`["uuid", …]`); chi tiết lấy qua `GET /v1/documents/{id}`. Tin chỉ có ảnh có `content` rỗng.
- Một trang có thể bắt đầu bằng tin `assistant` (câu hỏi của nó nằm ở trang trước).
- Hội thoại không tồn tại, `conversation_id` không phải UUID, hoặc của tài khoản khác đều trả `404 not_found`.
- Tiếp tục hội thoại: gửi `POST /v1/chat` với đúng `conversation_id` này. Chỉ gửi tối đa 48 message (`/v1/chat` trả `422` nếu nhiều hơn); service chỉ dùng câu hỏi cuối và tự nạp ngữ cảnh.

### `DELETE /v1/conversations/{conversation_id}`

Xoá một hội thoại (vd thao tác vuốt để xoá). Thành công: `204`, không có body. Không có, đã xoá rồi, hoặc của tài khoản khác: `404 not_found`.

Hội thoại đã xoá không mở lại được: hỏi tiếp bằng `conversation_id` đó thì Bụt không còn nhớ gì và lượt mới không được lưu. App mở hội thoại mới.

### `DELETE /v1/conversations`

Xoá **toàn bộ** lịch sử của tài khoản. Luôn trả `204` (kể cả khi không có gì để xoá), nên gọi lại an toàn.

**Xoá tài khoản:** app gọi API này **và** `DELETE /v1/documents` **trước khi** xoá tài khoản Supabase (xoá tài khoản xong thì token không dùng được nữa). Gọi lỗi (`503`, mất mạng) thì chưa xoá tài khoản, cho user thử lại.

## `POST /v1/report/monthly`

Body là object số liệu tổng hợp 2 tháng do app tự tính, tối đa 6000 ký tự khi serialize. Service không ép schema chi tiết: nhận nguyên khối, đưa vào prompt như dữ liệu.

Trả về `{"summary": "3–5 câu…"}`.

- Cùng số liệu thì trả bản đã cache (7 ngày), không gọi lại model.
- Không trừ lượt chat. Mỗi user tối đa 5 lần tạo mới mỗi ngày (`rate_limited`).
- Đoạn tóm tắt không chẩn đoán, không bàn đổi/ngừng thuốc, không nêu liều. Bản nào vi phạm thì bị chặn, trả `503 unavailable`, và app ẩn ô "Bụt tóm tắt".
- Cần `ai_chat_version` (đồng ý AI) như `/v1/chat`.

## `POST /v1/facility/suggest`

Gợi ý cơ sở khám theo triệu chứng (KAN-115). Hợp đồng đầy đủ: [`facility-suggest.md`](facility-suggest.md).

## Đồng ý (consent)

Đọc `user_consents` (cột `user_id`, `ai_chat_version`, `ai_share_profile`) qua PostgREST bằng anon key + token của user. Chỉ cache kết quả **đã đồng ý** (60 giây); chưa đồng ý thì không cache, nên app mở màn đồng ý rồi gửi lại ngay là được.

- `ai_chat_version` null → `403 consent_required`.
- Đặt `CONSENT_REQUIRED_VERSION` thì bắt buộc đúng version đó.
- Không đọc được bảng (chưa migrate, sai anon key, Supabase lỗi) → `503`. Không bao giờ coi là đã đồng ý.

## Deploy

1. Schema DB tự nâng cấp: mỗi lần `docker compose up -d --build`, service `migrate` chạy `alembic upgrade head` rồi app mới khởi động. Lỗi thì xem `docker compose logs migrate`. Lần đầu dùng Alembic trên DB cũ, revision `0001` thấy bảng đã có nên bỏ qua, chỉ ghi version.
2. `.env`: đặt `SUPABASE_URL`, `SUPABASE_ANON_KEY` (và `SUPABASE_JWT_SECRET` nếu project dùng HS256). `ROLLOUT_ALLOWLIST` giờ là danh sách UUID.
3. Trước khi migration `20261010010000_ai_chat_consent.sql` lên Supabase: `CONSENT_REQUIRED=false` (chỉ ở môi trường test; `APP_ENV=production` từ chối khởi động với giá trị này).
4. DNS (bản ghi `A` `ai` → IP server) và chứng chỉ `ai.but.care` đã có sẵn.
5. nginx: copy lại `deploy/nginx/bcare-ai.conf` (thêm `location /v1/`), `nginx -t && systemctl reload nginx`.
