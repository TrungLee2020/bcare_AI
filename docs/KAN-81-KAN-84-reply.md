# Trả lời KAN-81 và KAN-84 (phía AI)

Gửi team App, 2026-10-10. Đặc tả đầy đủ phía service: [`docs/v1-app-api.md`](v1-app-api.md) (repo bcare_AI, nhánh `feat/v1-app-api`).

---

## KAN-84 — Domain HTTPS cho Trợ lý Bụt

**Chốt:** `AI_CHAT_URL=https://chat.but.care`

| Việc | Trạng thái |
|---|---|
| Code `/v1/chat`, `/v1/report/monthly` trên server | Xong |
| Cấu hình nginx cho `chat.but.care` (TLS 1.2/1.3, không buffer SSE, timeout 150s) | Xong, chờ chứng chỉ |
| Bản ghi DNS `chat` → `14.225.29.169` (nhà đăng ký P.A Việt Nam) | **Đang làm** |
| Chứng chỉ Let's Encrypt, tự gia hạn | Cấp ngay khi DNS có hiệu lực |

- App cứ build với URL trên, khi domain lên không phải sửa gì.
- Trước lúc đó `chat.but.care` chưa phân giải được, app sẽ gặp lỗi mạng.
- Sẵn sàng khi `curl https://chat.but.care/health` trả `{"status":"ok",...}`. Phía AI sẽ báo khi xong.

---

## KAN-81 — Báo cáo sức khoẻ tháng: "Bụt tóm tắt"

**Chốt:** `POST /v1/report/monthly` trả `{"summary": "3–5 câu…"}` đúng đặc tả. Code xong, có test.

**Request**
- Header `Authorization: Bearer <access token Supabase>`.
- Body là object số liệu tổng hợp 2 tháng do app tự tính (huyết áp TB và số lần đo cao, nhịp tim, cân nặng, tỉ lệ uống thuốc, triệu chứng, lần khám, xét nghiệm…). Tối đa 6000 ký tự khi serialize.
- Service không ép tên trường: nhận nguyên khối và đưa vào prompt như dữ liệu. App giữ đúng tên trường đang dùng.
- Không gửi nhật ký thô. Đúng như app đang làm.

**Response**
```json
{"summary": "Huyết áp trung bình tháng này là 138/86, giảm so với 145/90 tháng trước. ..."}
```

**Hành vi**
- Tiếng Việt, 3–5 câu, chỉ dùng số liệu có trong body.
- Không chẩn đoán, không khuyên đổi/ngừng thuốc, không nêu liều. Có chỉ số đáng lo thì khuyên trao đổi với bác sĩ.
- Bản tóm tắt vi phạm bị chặn và trả `503 unavailable` → app **ẩn ô "Bụt tóm tắt"**, phần quy tắc cố định vẫn hiện bình thường.
- Cùng số liệu thì trả bản cache (7 ngày), không gọi lại model.
- **Không trừ lượt chat.** Mỗi user tối đa 5 lần tạo mới mỗi ngày (`429 rate_limited`).
- Cần user đã đồng ý Trợ lý AI (`user_consents.ai_chat_version`), giống `/v1/chat`. Khớp với việc app chỉ hiện ô này khi Trợ lý AI bật.

**Lỗi** (body `{"error","message"}`)

| HTTP | `error` | App làm gì |
|---|---|---|
| 401 | `unauthorized` | Refresh session / đăng nhập lại |
| 403 | `consent_required` / `not_enabled` | Ẩn ô, **không đăng xuất** |
| 413 / 422 | `payload_too_large` / `invalid_request` | Lỗi phía app, ẩn ô |
| 429 | `rate_limited` | Ẩn ô, thử lại hôm sau |
| 503 | `unavailable` | Ẩn ô, có thể thử lại sau |

---

## Phía App cần làm (chung cho cả hai)

1. **Deploy migration** `20261010010000_ai_chat_consent.sql` lên Supabase. Chưa có bảng `user_consents` thì mọi request trả `503`.
2. **Gửi `service_role` key** (Supabase → Project Settings → API) cho phía AI qua **tin nhắn riêng**, không gửi vào group hay ticket. Key chỉ đặt trong `.env` trên server. Lý do và phương án thay thế: mục "Cấu hình Supabase phía backend" trong `v1-app-api.md`.
3. **Set `app_metadata.tier = "premium"`** cho user premium (bằng service role). Không set thì tính là free: 2 câu chat/ngày, premium 5 câu/ngày.
4. **Xử lý lỗi theo trường `error`**: chỉ `401` là hết phiên, `403` không đăng xuất.

Khi DNS và chứng chỉ xong, phía AI báo lại để test end-to-end trên build `AI_CHAT=true`, `MONTHLY_REPORT=true`.
