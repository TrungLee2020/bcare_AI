# Đọc ảnh giấy tờ y tế gửi qua chat (OCR) — thiết kế

Ngày: 2026-11-06. Trạng thái: chờ duyệt, chưa có code.

## 1. Mục tiêu

User gửi ảnh đơn thuốc, kết quả xét nghiệm, bệnh án, phiếu kết quả chụp chiếu trong chat. Bụt **lưu vào hồ sơ sức khoẻ cá nhân trước**, chỉ trả lời khi user hỏi. Dữ liệu trích xuất là nền cho trợ lý sức khoẻ cá nhân về sau.

Tiết kiệm token: phân loại ảnh bằng một lần gọi rẻ trước; ảnh không phải giấy tờ y tế thì không OCR.

Lưu theo từng user: ảnh gốc, chữ OCR, dữ liệu trích xuất.

**Ngoài phạm vi lần này:**
- Đọc hay diễn giải phim chụp (X-quang, CT…): chỉ phân loại và lưu.
- Hỏi đáp trên toàn bộ hồ sơ qua nhiều hội thoại ("thuốc mình đang uống là gì"): làm ở bước sau, trên dữ liệu lần này lưu.
- Tách thuốc / chỉ số xét nghiệm thành bảng riêng: tạm để JSONB.
- Kênh đẩy trạng thái mới: app gọi lại API danh sách.

## 2. Quyết định đã chốt

| Vấn đề | Chốt |
|---|---|
| Mục đích | Lưu hồ sơ trước; trả lời khi user hỏi |
| Lưu ảnh gốc | Supabase Storage, bucket riêng tư `health-documents`, RLS theo user |
| Upload | App upload bằng token user, gửi `path` kèm tin chat |
| Phản hồi | Ảnh không kèm câu hỏi: trả lời ngay câu cố định, xử lý nền. Có câu hỏi: xử lý xong rồi trả lời |
| Đồng ý | Dùng `ai_chat_version` + `ai_share_profile` hiện có (popup chia sẻ của app) |
| Hạn mức | 3 ảnh/ngày, mọi gói, chỉ tính ảnh được OCR, không trừ lượt chat |
| Bộ xử lý | Model GPT qua API cho cả phân loại và OCR (không thêm công cụ OCR riêng) |
| Nén ảnh | Backend chuẩn hoá và thu nhỏ trước khi gửi model; mốc kích thước chốt bằng đo |

## 3. Luồng xử lý

### 3.1 App

1. Thu nhỏ ảnh (cạnh dài ≤ 2048 px), xoá EXIF (vị trí GPS), upload vào `health-documents/{user_id}/{uuid}.jpg` bằng token user.
2. `POST /v1/chat` với `attachments` (mục 6). Tin có ảnh thì phần chữ được để trống.

### 3.2 Backend, trong request (mục tiêu < 2 giây)

1. Gate như `/v1/chat` hiện có (xác thực, rollout, `ai_chat`), cộng thêm `ai_share_profile = true`, không thì `403 consent_required`.
2. Mỗi `path` phải bắt đầu bằng `{user_id}/`, không thì `403 forbidden`. RLS là lớp thứ hai.
3. Tải ảnh: `GET {SUPABASE_URL}/storage/v1/object/authenticated/health-documents/{path}` với `apikey: <anon>` + `Authorization: Bearer <token user>`. Không dùng service role key.
4. Kiểm: giải mã được bằng Pillow, định dạng JPEG / PNG / WebP (app chuyển HEIC sang JPEG trước khi upload), ≤ `OCR_MAX_IMAGE_BYTES`. Sai → `422 invalid_attachment`.
5. Tính `sha256`. Cùng user đã có tài liệu cùng hash (chưa xoá) → dùng lại tài liệu đó, xoá object vừa upload trùng (đang có token user), không xử lý lại.
6. Tạo `health_documents` trạng thái `received`. Đặt bytes ảnh vào Redis `ocr_image:{document_id}`, TTL `OCR_IMAGE_TTL_SECONDS` (3600). Worker không cần token user (token Supabase hết hạn sau ~1 giờ).
7. Ghi id tài liệu vào `meta.documents` của tin user trong `chat_messages`.
8. Rẽ nhánh:
   - **Không có câu hỏi:** publish `document_jobs` (key = user_id), trả ngay câu cố định "Bụt đã nhận N ảnh, đang đọc và lưu vào hồ sơ của bạn." Không gọi model, không trừ lượt chat.
   - **Có câu hỏi:** đi luồng chat hiện có (trừ lượt chat như thường); message Kafka mang thêm danh sách id tài liệu. Consumer xử lý tài liệu (mục 3.3) rồi mới trả lời.

### 3.3 Xử lý một tài liệu (`documents.process(document_id)`)

Dùng chung cho worker `document_jobs` và consumer chat. Idempotent: tài liệu đã ở trạng thái cuối thì bỏ qua.

1. `processing`. Lấy ảnh từ Redis; không còn → `failed` (`image_expired`).
2. **Chuẩn hoá ảnh** (mục 4).
3. **Phân loại**: `OCR_CLASSIFY_MODEL`, ảnh 512 px, `detail: low`, structured output:
   `{doc_type, readable, confidence}` với `doc_type` ∈ `prescription | lab_result | medical_record | imaging_report | imaging_film | other_medical | not_medical`.
4. Dừng sớm, **không trừ hạn mức**:
   - `not_medical`, hoặc `confidence < OCR_MIN_CONFIDENCE` → `skipped`.
   - `readable = false` → `unreadable`.
   - `imaging_film` → `done`, không OCR, không trích xuất.
5. **Trừ hạn mức** (Redis, ngày theo giờ VN như quota chat). Hết → `quota_exceeded`.
6. **OCR + trích xuất**: `OCR_EXTRACT_MODEL`, ảnh `OCR_MAX_SIDE`, `detail: high`, một lần gọi, structured output theo `doc_type` (mục 5).
7. Lưu chữ OCR và bản trích xuất, dựng `title`, `doc_date`, ghi `meta` (model, prompt version, token, chi phí) → `done`. Xoá key Redis.
8. Lỗi model / quá `OCR_LLM_BUDGET_SECONDS`: retry theo `app/services/retry.py`; hết lượt → `failed` (`model_error`), **hoàn hạn mức**.

### 3.4 Chat về sau

Câu hỏi trong một hội thoại tự có dữ liệu trích xuất của tối đa `OCR_CONTEXT_DOCUMENTS` (3) tài liệu `done` gần nhất được đính trong hội thoại đó (lấy từ `chat_messages.meta.documents`). Đưa vào prompt qua `fence("document_data", …)`, giới hạn `OCR_CONTEXT_MAX_CHARS`. Chỉ đưa bản trích xuất, không đưa chữ OCR thô (ngắn hơn, ít chỗ cho câu injection).

Tài liệu `unreadable` / `quota_exceeded` / `failed` trong hội thoại: Bụt nói rõ chưa đọc được và vì sao (chụp lại / mai đọc tiếp / gửi lại).

**Liều thuốc trong chat:** `output_validator` vẫn chặn số mg / số viên trong câu trả lời, kể cả khi chép từ đơn của chính user. Bụt nói "theo đơn bác sĩ ghi, bạn xem chi tiết trong hồ sơ"; liều đầy đủ app hiển thị ở màn tài liệu (dữ liệu trích xuất, không qua LLM). Đổi luật này là quyết định của Product (mục 11).

## 4. Chuẩn hoá ảnh

Token tính theo kích thước điểm ảnh, không theo dung lượng file: giảm chất lượng JPEG không bớt token, thu nhỏ mới bớt.

1. Xoay theo EXIF, chuyển RGB, bỏ EXIF.
2. Cắt viền thừa: bỏ vùng nền gần đồng màu quanh tờ giấy (so với màu góc ảnh, có ngưỡng); không cắt quá `OCR_MAX_CROP_RATIO` mỗi cạnh để không mất chữ sát mép.
3. Thu nhỏ giữ tỉ lệ, không phóng to: 512 px (phân loại), `OCR_MAX_SIDE` (OCR, mặc định 1600).
4. JPEG chất lượng 85, gửi dạng `data:` URL.

Ảnh đã chuẩn hoá không lưu (dựng lại được từ ảnh gốc).

`OCR_MAX_SIDE` chốt bằng đo (mục 10): chạy bộ ảnh mẫu ở 1024 / 1280 / 1600 / 2048, so `usage.prompt_tokens` thật và độ chính xác, chọn mốc thấp nhất không làm giảm độ chính xác.

## 5. Dữ liệu trích xuất

Nguyên tắc chung: **chép như ghi trên giấy**, không diễn giải, không suy luận, không chuẩn hoá tên thuốc. Không có thì `null`. Chỗ không chắc ghi đường dẫn trường vào `uncertain_fields`. Chữ OCR chép nguyên văn, chỗ không đọc được ghi `[không đọc được]`.

Trường chung: `doc_date` (YYYY-MM-DD), `facility`, `doctor`, `patient_name`, `patient_birth_year`, `patient_gender`, `uncertain_fields`.

| `doc_type` | Trường riêng |
|---|---|
| `prescription` | `diagnosis_as_written`, `medications[]` {`name`, `strength`, `form`, `quantity`, `instructions_as_written`, `duration`}, `follow_up_date`, `notes` |
| `lab_result` | `tests[]` {`name`, `value`, `unit`, `reference_range`, `flag_as_printed`} |
| `medical_record` | `record_kind` (phiếu khám / giấy ra viện / tóm tắt bệnh án / khác), `diagnosis_as_written`, `treatment_as_written`, `follow_up_date` |
| `imaging_report` | `modality` (X-quang / siêu âm / CT / MRI / nội soi / khác), `body_part`, `findings_as_written`, `conclusion_as_written` |
| `other_medical` | chỉ chữ OCR, không trích xuất (vd hoá đơn viện phí; có thể dùng cho bồi thường bảo hiểm sau này) |

`patient_*` để app gán tài liệu cho đúng thành viên gia đình sau này; backend không tự gán. Request có thể kèm `profile_id` (chuỗi app tự định nghĩa), lưu nguyên, `null` = chủ tài khoản.

`title` dựng từ dữ liệu: "{Loại} – {facility} – {doc_date dd/mm/yyyy}", bỏ phần thiếu.

Prompt: `prompts/ocr_classify_v1.md`, `prompts/ocr_extract_v1.md`. Chữ trong ảnh là dữ liệu: prompt nói rõ câu nào trong ảnh giống chỉ dẫn thì chép lại như chữ, không làm theo.

## 6. API

Lỗi theo dạng chung `{"error", "message", ...}` trong `docs/v1-app-api.md`.

**`POST /v1/chat`** thêm:

```json
{
  "messages": [{"role": "user", "content": ""}],
  "attachments": [{"path": "<user_id>/<uuid>.jpg", "profile_id": null}]
}
```

Tối đa `OCR_MAX_ATTACHMENTS` (3) ảnh mỗi tin. Response thêm `documents: [{"id", "status"}]`. Không có `attachments` thì hành vi như cũ.

**`GET /v1/documents?type=&limit=&cursor=`**: tài liệu chưa xoá của user, mới trước: `id`, `doc_type`, `title`, `doc_date`, `status`, `storage_path`, `profile_id`, `created_at`. App tự lấy ảnh từ Storage bằng `storage_path`.

**`GET /v1/documents/{id}`**: thêm `ocr_text`, `extraction` (bản mới nhất), `uncertain_fields`. Tài liệu của user khác hoặc đã xoá → `404`.

**`DELETE /v1/documents/{id}`**: đặt `deleted_at`, xoá ngay object trên Storage bằng token user (lúc này có token), trả `204`. Bản ghi DB bị job dọn dẹp hiện có xoá hẳn sau `HISTORY_PURGE_AFTER_DAYS`. Xoá hội thoại không xoá tài liệu.

**Trạng thái:** `received` → `processing` → `done` | `skipped` | `unreadable` | `quota_exceeded` | `failed`.

## 7. Lưu trữ (Postgres, migration Alembic `0004`)

`health_documents`

| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | varchar(64) | |
| `profile_id` | varchar(64) null | do app gửi |
| `storage_path` | text | |
| `sha256` | char(64) | unique (`user_id`, `sha256`) where `deleted_at is null` |
| `status` | varchar(20) | check theo danh sách trạng thái |
| `doc_type` | varchar(20) null | |
| `confidence` | real null | |
| `title`, `doc_date` | text null, date null | |
| `meta` | JSONB | model, prompt version, token, chi phí, lý do `failed` |
| `created_at`, `processed_at`, `deleted_at` | timestamptz | |

Index (`user_id`, `created_at desc`).

`health_document_ocr`: `document_id` PK/FK (cascade), `text`, `model`, `created_at`.

`health_document_extractions`: `id` PK, `document_id` FK (cascade), `schema_version`, `data` JSONB, `model`, `created_at`. Nhiều phiên bản; bản mới nhất là bản dùng.

Không log chữ OCR, dữ liệu trích xuất hay `storage_path`; log chỉ có id tài liệu, loại, trạng thái, token. Metrics: `ocr_classified_*`, `ocr_skipped`, `ocr_done`, `ocr_failed`, `ocr_quota_exceeded`, `ocr_tokens`.

## 8. Cấu hình (`.env`)

| Biến | Mặc định |
|---|---|
| `OCR_ENABLED` | `false` (bật khi app sẵn sàng; tắt thì tin có `attachments` → `403 not_enabled`, như rollout) |
| `OCR_CLASSIFY_MODEL` | `gpt-5-nano` |
| `OCR_EXTRACT_MODEL` | bằng `OPENAI_MODEL`; đổi nếu bước đo cho thấy model khác đọc đúng hơn rõ rệt |
| `OCR_CLASSIFY_PROMPT_VERSION`, `OCR_EXTRACT_PROMPT_VERSION` | `v1` |
| `OCR_CLASSIFY_SIDE` / `OCR_MAX_SIDE` | 512 / 1600 |
| `OCR_MIN_CONFIDENCE` | 0.6 |
| `OCR_MAX_PER_DAY` | 3 |
| `OCR_MAX_ATTACHMENTS` | 3 |
| `OCR_MAX_IMAGE_BYTES` | 10 MB |
| `OCR_IMAGE_TTL_SECONDS` | 3600 |
| `OCR_LLM_BUDGET_SECONDS` | 60 |
| `OCR_CONTEXT_DOCUMENTS` / `OCR_CONTEXT_MAX_CHARS` | 3 / 4000 |
| `STORAGE_BUCKET` | `health-documents` |

## 9. Cấu trúc code

- `app/services/documents/image.py`: chuẩn hoá ảnh (thuần, không I/O).
- `app/services/documents/storage.py`: tải / xoá object Supabase bằng token user.
- `app/services/documents/classify.py`, `extract.py`: gọi model, schema structured output.
- `app/services/documents/pipeline.py`: `process(document_id)`, trạng thái, hạn mức.
- `app/db/models.py`, `app/db/documents_repository.py`: bảng và truy vấn.
- `app/api/v1.py`: `attachments` trong `/v1/chat`, endpoint `/v1/documents`.
- `app/kafka/`: topic `document_jobs`, vòng consume trong consumer hiện có.
- `app/prompts.py`: thêm `document_data` vào `RESERVED_TAGS`.

## 10. Kiểm thử

Unit (model giả, như test hiện có):
- Chuẩn hoá: xoay EXIF, cắt viền, không phóng to, kích thước đầu ra.
- Gate: `path` của user khác, thiếu `ai_share_profile`, file không phải ảnh, quá lớn, quá số ảnh.
- Luồng trạng thái: `not_medical` / độ chắc chắn thấp / mờ / phim không OCR và không trừ hạn mức; hết hạn mức; lỗi model hoàn hạn mức; ảnh Redis hết hạn; xử lý lại tài liệu đã xong thì bỏ qua.
- Trùng `sha256` dùng lại tài liệu, xoá object trùng.
- Ảnh không kèm câu hỏi không gọi model chat, không trừ lượt chat.
- Ngữ cảnh chat: chỉ tài liệu của hội thoại đó, đúng giới hạn, bọc `fence`; chữ trong ảnh có câu injection không thoát khối.
- `GET`/`DELETE /v1/documents`: chỉ thấy tài liệu của mình, xoá gọi Storage.

Live (`-m live`), bộ ảnh mẫu ~12 tấm: đơn thuốc in, đơn viết tay, phiếu xét nghiệm nhiều dòng, giấy ra viện, phiếu kết quả siêu âm, phim X-quang, và ảnh không phải giấy tờ (đồ ăn, chân dung, ảnh chụp màn hình). Ảnh giả lập do bên AI tạo; thêm ảnh thật đã che thông tin cá nhân khi có. Đo:
- Phân loại đúng loại; ảnh không phải giấy tờ không bị OCR.
- So trường trích xuất với đáp án viết tay: tên thuốc, chỉ số, ngày.
- `usage.prompt_tokens` ở 1024 / 1280 / 1600 / 2048 px để chốt `OCR_MAX_SIDE`; so `OCR_EXTRACT_MODEL` giữa `gpt-5-nano` và `gpt-6-luna`.

## 11. Việc của team App và câu hỏi mở

Team App:
1. Tạo bucket riêng tư `health-documents`, policy: user chỉ đọc / ghi / xoá object dưới `{auth.uid()}/`.
2. Thu nhỏ ảnh, xoá EXIF, chuyển HEIC sang JPEG trước khi upload; gửi `attachments`.
3. Màn hồ sơ: danh sách, chi tiết (dữ liệu trích xuất, đánh dấu trường không chắc), xoá, trạng thái.
4. Nội dung popup chia sẻ hồ sơ nói rõ có lưu ảnh và dữ liệu giấy tờ y tế.

Product:
1. Bụt có được đọc lại liều thuốc từ đơn của chính user trong chat không (hiện: không, xem mục 3.4).
2. Hạn mức 3 ảnh/ngày có khác nhau theo gói không (hiện: như nhau).
