# Gợi ý cơ sở khám theo triệu chứng (`/v1/facility/suggest`) — KAN-115

**Trạng thái (2026-10-30):** đã code trên nhánh `feat/v1-app-api`, chưa deploy. Cần team App duyệt hợp đồng (mục 2–4) và Product chốt các câu ở mục 7; chỗ nào chưa chốt thì service đang chạy theo đề xuất ghi ở đó.

Code: [`app/services/facility/`](../app/services/facility/), endpoint trong [`app/api/v1.py`](../app/api/v1.py), prompt [`prompts/facility_v1.md`](../prompts/facility_v1.md), dữ liệu [`app/facility_data/`](../app/facility_data/), test [`tests/test_facility.py`](../tests/test_facility.py).

Nguồn: ticket KAN-115, dữ liệu và đề xuất của team App (đo trên DB live 2026-10-29), các file trong [`jsons/`](../jsons/).

## 1. Phạm vi

AI chỉ gợi ý **nơi khám**. Không chẩn đoán, không nói người dùng bị bệnh gì. Khi không chắc thì nói rõ là không chắc, không lấp danh sách cho đủ.

Chọn phương án **service lọc và xếp hạng** (không phải phương án "AI chỉ trả chuyên khoa, app tự lọc"):
- Luật lọc, lý do gợi ý và trạng thái trống nằm ở một chỗ, sửa không cần ra bản app mới.
- Product còn đổi bảng map và luật tuổi; đổi ở service là có hiệu lực ngay.
- Đổi lại, app phải gửi toạ độ đã làm tròn (~100 m). Service **không lưu và không log** toạ độ.

Chia việc trong service:

| Bước | Làm bằng | Ghi chú |
|---|---|---|
| 1. Chuẩn hoá triệu chứng | Thẻ app gửi: dùng thẳng. Câu gõ tự do: LLM, chỉ được trả key trong `symptom_tags.json` | LLM không được trả key lạ; key lạ bị bỏ |
| 2. Map sang chuyên khoa | Code, tra `symptom_specialty_map` | LLM chỉ góp phần "bối cảnh" (vd. *sau té ngã*), không tự chọn chuyên khoa |
| 3. Lọc cứng (chuyên khoa + tuổi) | Code | Không bao giờ giao cho LLM |
| 4. Xếp hạng + lý do | Code | Lý do sinh từ dữ liệu, không do LLM viết |

Dấu hiệu cấp cứu được dò bằng hai đường độc lập: từ khoá (chạy cả khi LLM lỗi) và LLM. Một trong hai thấy là trả `emergency`. Từ khoá không hiểu phủ định ("không méo miệng" vẫn khớp); chấp nhận, vì báo nhầm cấp cứu ít hại hơn bỏ sót.

Mỗi triệu chứng LLM trả về phải kèm đoạn chữ nguyên văn của người dùng làm căn cứ (`matched_text`). Căn cứ không có trong câu gõ thì triệu chứng bị bỏ.

## 2. Request

`POST /v1/facility/suggest`, xác thực như `/v1/chat` (`Authorization: Bearer <access token Supabase>`). Cần đã đồng ý Trợ lý AI (`ai_chat_version`), vì câu gõ tự do được gửi cho model, và tài khoản phải nằm trong nhóm rollout như `/v1/chat` (ngoài nhóm → `403 not_enabled`).

```json
{
  "profile": { "age": 36 },
  "symptom_tags": ["headache", "dizziness"],
  "text": "nhức đầu mấy hôm nay, đứng lên hay choáng",
  "location": { "lat": 10.776, "lng": 106.700 },
  "radius_km": 10,
  "limit": 10,
  "clarify_choice": null
}
```

| Trường | Bắt buộc | Ý nghĩa |
|---|---|---|
| `profile.age` | Có | Tuổi của **hồ sơ đang chọn** (không phải chủ tài khoản), số nguyên 0–120. `≤ 16` là trẻ em. Không gửi ngày sinh, tên. |
| `symptom_tags` | Một trong hai | Key trong `symptom_tags.json`. Key lạ → `422`. |
| `text` | Một trong hai | Câu gõ tự do, tối đa 500 ký tự. |
| `location` | Có | Toạ độ đã làm tròn. |
| `radius_km` | Không | Mặc định 10, tối đa 50. |
| `limit` | Không | Mặc định 10, tối đa 30. |
| `clarify_choice` | Không | Gửi lại khi trả lời câu hỏi làm rõ (mục 3.3): `value` của lựa chọn người dùng bấm, kèm nguyên request cũ. Nhận key chuyên khoa trong `specialties.json` hoặc `general_only`; giá trị khác → `422`. Kết quả đọc câu gõ được cache 24 giờ theo nội dung câu, nên lượt này thường không gọi lại model. |

## 3. Response

Luôn `200` với một trong 4 `status`: `ok`, `empty`, `clarify`, `emergency`. Thứ tự ưu tiên: `emergency` → `clarify` → `ok` / `empty`. Lỗi dùng chung bảng lỗi trong [`v1-app-api.md`](v1-app-api.md#lỗi): `422 invalid_request` (thẻ lạ, thiếu cả thẻ lẫn câu gõ, câu gõ > 500 ký tự, `clarify_choice` lạ), `429 rate_limited`, `503 unavailable` (chưa tải được bảng cơ sở).

Mọi response có `interpretation` và `data_version` (`max(updated_at)` của bảng cơ sở đang dùng).

`interpretation` để app hiện "Bụt hiểu là…":

```json
"interpretation": {
  "age_group": "adult",
  "symptoms": [
    { "key": "headache", "name": "Đau đầu", "source": "tag" },
    { "key": "dizziness", "name": "Chóng mặt", "source": "text", "matched_text": "hay choáng" }
  ],
  "specialties": [
    { "key": "than_kinh", "label": "Nội thần kinh", "role": "primary", "from": ["headache", "dizziness"] }
  ],
  "fallback_general": true,
  "unmapped_text": [],
  "note": null
}
```

- `role`: `primary` | `secondary`. Đa khoa không nằm trong danh sách này; `fallback_general: true` nghĩa là cơ sở đa khoa được dùng làm dự phòng (hiện luôn true).
- `from`: key triệu chứng dẫn tới chuyên khoa, hoặc `mental` (người dùng nói rõ vấn đề tinh thần), `choice` (người dùng chọn ở câu hỏi làm rõ).
- `unmapped_text`: phần câu gõ không khớp thẻ nào (vd. `["đau răng"]`).
- `note`: câu giải thích khi đang gợi ý chung (không có chuyên khoa nào), khi một phần mô tả chưa có trong bảng, hoặc khi chưa đọc được câu gõ (LLM lỗi). `null` khi không có gì cần nói.

### 3.1 `ok`

```json
{
  "status": "ok",
  "interpretation": { "...": "..." },
  "facilities": [
    {
      "id": "uuid",
      "name": "Bệnh viện Đại học Y Dược TP.HCM",
      "lat": 10.755, "lng": 106.664,
      "address": null,
      "city": "Hồ Chí Minh",
      "distance_km": 3.8,
      "match": "primary",
      "matched_specialties": ["than_kinh"],
      "audience": "all",
      "reason": "Đau đầu, Chóng mặt → Nội thần kinh · Phù hợp: người lớn"
    },
    {
      "id": "uuid",
      "name": "Bệnh viện Quận 1",
      "distance_km": 1.2,
      "match": "general",
      "matched_specialties": [],
      "audience": "all",
      "reason": "Đa khoa: khám ban đầu, bác sĩ chuyển đúng chuyên khoa nếu cần · Phù hợp: người lớn"
    }
  ],
  "data_version": "2026-10-27T08:12:00Z"
}
```

- `match`: `primary` → `secondary` → `pediatric` (chỉ với trẻ em: cơ sở Nhi không có đúng khoa) → `general`. Thứ tự hiển thị đúng như mảng trả về.
- `audience`: `child` | `adult` | `all`.
- `reason` là câu tiếng Việt sinh từ dữ liệu, app hiện nguyên văn.
- `id` để ghép ghim bản đồ và nút chỉ đường.

### 3.2 `empty`

Không có nơi nào qua lọc cứng trong bán kính. Service **không** lấp bằng cơ sở sai khoa hoặc sai đối tượng.

```json
{
  "status": "empty",
  "interpretation": { "...": "..." },
  "facilities": [],
  "empty": {
    "message": "Chưa tìm thấy nơi khám Nội thần kinh hoặc đa khoa cho người lớn trong 10 km.",
    "suggestions": [
      { "kind": "widen_radius", "radius_km": 25, "label": "Tìm trong 25 km" }
    ]
  }
}
```

Đa khoa đã luôn nằm trong danh sách (dự phòng), nên `empty` nghĩa là trong bán kính không có cả cơ sở đa khoa phù hợp. Gợi ý duy nhất là nới bán kính theo các mốc 10 → 25 → 50 km; đã ở 50 km thì `suggestions` rỗng. App bấm gợi ý thì gửi lại request với `radius_km` mới.

### 3.3 `clarify`

Người dùng nói "thần kinh" mà không rõ là triệu chứng cơ thể hay sức khoẻ tinh thần (vd. "tôi muốn khám thần kinh"). Service không tự chọn khoa. Khi người dùng nói rõ vấn đề tinh thần (lo âu, trầm cảm…) thì không hỏi lại mà thêm `tam_than` làm chuyên khoa chính.

```json
{
  "status": "clarify",
  "interpretation": { "...": "..." },
  "clarify": {
    "question": "Bạn đang gặp vấn đề nào?",
    "options": [
      { "value": "than_kinh", "label": "Triệu chứng cơ thể: đau đầu, chóng mặt, tê bì…" },
      { "value": "tam_than", "label": "Tinh thần: lo âu, buồn bã kéo dài, căng thẳng…" },
      { "value": "general_only", "label": "Chưa rõ, gợi ý đa khoa" }
    ]
  },
  "facilities": []
}
```

### 3.4 `emergency`

Có dấu hiệu cấp cứu (`red_flag` trong bảng map, hoặc nhóm `dot_quy`). Không lọc chuyên khoa.

```json
{
  "status": "emergency",
  "emergency": {
    "kind": "stroke",
    "message": "Méo miệng, yếu nửa người, nói đớ có thể là dấu hiệu đột quỵ. Gọi 115 ngay hoặc đến cơ sở cấp cứu gần nhất.",
    "call": "115"
  },
  "interpretation": { "...": "..." },
  "facilities": [ { "id": "uuid", "name": "…", "distance_km": 1.2, "match": "general", "reason": "Cơ sở đa khoa gần nhất · Phù hợp: người lớn" } ]
}
```

- `kind`: `stroke` | `chest_pain` | `breathing` | `head_injury`. Cả 4 loại đang bật; `chest_pain`, `breathing`, `head_injury` chờ Product xác nhận (mục 7).
- `facilities`: 3 cơ sở đa khoa gần nhất hợp đối tượng, không giới hạn bán kính. DB chưa có cột "có cấp cứu", nên đây là gần đúng.

## 4. Lọc và xếp hạng

**Bỏ hẳn** (không bao giờ gợi ý):
- Cơ sở chưa gắn gì: `is_general = false` và `specialties` rỗng (400 dòng).
- Viện Pháp y Tâm thần.

**Sửa tạm** theo [`app/facility_data/facility_overrides.json`](../app/facility_data/facility_overrides.json) (dựng từ `facility_data_issues.json`) **cho tới khi DB được sửa**:
- 43 dòng `is_general` sai: bỏ cờ đa khoa, gán khoa suy từ tên (Nhi TW → `nhi`, Mắt TW → `mat`, Tâm thần TW → `tam_than`…). Nhiệt đới, Lão khoa, Phục hồi chức năng, MEDLATEC chưa có key phù hợp nên bị ẩn.
- 3 BV Tâm thần: bỏ tag `than_kinh`.
- 17 cơ sở chỉ khám trẻ em: `audience = child`.

Thêm vài luật theo tên làm lưới an toàn cho dòng chưa có trong danh sách trên. Luật nào cũng chỉ bớt chỗ cơ sở có thể xuất hiện, không bao giờ thêm:
- tên có "tâm thần" → bỏ `than_kinh`, thêm `tam_than`, bỏ cờ đa khoa;
- tên có "xét nghiệm" → bỏ cờ đa khoa;
- tên có "nhi" hoặc "trẻ em" mà không có "sản" → `audience = child` (so khớp có dấu, nên "Nhị" không bị tính);
- tên có "pháp y" → ẩn.

**Lọc cứng**, giữ cơ sở thoả **đồng thời**:
1. Có chuyên khoa đã map (`specialties` giao với `primary ∪ secondary`), **hoặc** là đa khoa (`is_general`), **hoặc** (chỉ với trẻ em) là cơ sở Nhi.
2. Đối tượng khớp: người lớn → bỏ `audience = child`; trẻ em → bỏ `audience = adult`. Chưa có cột thì coi là `all`, trừ các dòng sửa tạm và luật theo tên ở trên.
3. Trong `radius_km`, tính bằng haversine từ `lat/lng`.

**Xếp hạng**:
- Người lớn: `primary` → `secondary` → `general`; cùng nhóm thì gần hơn trước.
- Trẻ em: cơ sở Nhi (`audience = child` hoặc có tag `nhi`) có đúng khoa → khoa đúng ở nơi khác → cơ sở Nhi bất kỳ → đa khoa; cùng nhóm thì gần hơn trước.

**Bối cảnh trong câu gõ** (LLM đọc, code áp dụng):
- Sau té ngã / va đập / tai nạn: `joint_pain`, `back_pain`, `headache`, `numbness` → `ngoai_chan_thuong` chính, chuyên khoa gốc lùi xuống phụ.
- Người dùng nói rõ vấn đề tinh thần → thêm `tam_than` chính.

**Triệu chứng chưa có trong bảng map**: gợi ý đa khoa, ghi `note`, và ghi log cụm từ chưa map (`facility_unmapped phrase=…`, không kèm user id, không kèm toạ độ) để team bổ sung bảng. Không map theo chữ gần giống.

## 5. Dữ liệu cơ sở

Service đọc `facilities` qua Supabase REST bằng anon key (đã có `SUPABASE_URL`, `SUPABASE_ANON_KEY` trong `.env`):

```
GET /rest/v1/facilities?select=id,name,lat,lng,brand_kind,specialties,is_general,updated_at
    &category=eq.hospital&is_active=eq.true&order=id&limit=1000&offset=0
```

- Cache cả bảng trong bộ nhớ của từng instance (đo 2026-10-30: 1.360 / 1.778 dòng dùng được sau khi làm sạch). Mỗi `FACILITY_REFRESH_SECONDS` (mặc định 600) hỏi `max(updated_at)`; đổi thì kéo lại.
- Supabase lỗi thì dùng bản cũ; chưa từng có bản nào thì `503 unavailable`.
- Khi cột `audience` đã lên DB: đặt `FACILITY_AUDIENCE_COLUMN=true`, service thêm cột vào `select`.

## 6. Hạn mức và quyền riêng tư

- **Không trừ lượt chat.** Giới hạn riêng `FACILITY_MAX_PER_DAY` (mặc định 30) lượt/ngày/user, tính mọi lượt hợp lệ (`429 rate_limited`). Lượt chỉ có thẻ (không `text`) không gọi LLM.
- Không lưu và không log toạ độ. Kết quả đọc câu gõ (thẻ + cờ, không phải nguyên câu) được cache 24 giờ trong Redis theo hash của câu, không kèm user id. Log chỉ có cụm từ chưa map (mục 4) và số liệu tổng trên `/metrics` (`facility_ok`, `facility_empty`, `facility_clarify`, `facility_emergency_*`, `facility_unmapped`…).
- `text` đi qua bộ lọc đầu vào như `/v1/chat` (chặn prompt injection).

## 7. Cần Product chốt (@Tristan)

Từ bản nháp của team App:
1. **Mất ngủ:** đang chạy: mặc định Nội thần kinh; người dùng nói rõ lo âu / trầm cảm / căng thẳng thì thêm Tâm thần làm chính; chỉ hỏi lại khi người dùng nói "thần kinh" chung chung.
2. **Trẻ sưng đau khớp:** đề xuất Cơ xương khớp là chính, Tim mạch phụ; kèm sốt thì Tim mạch lên chính (nghi thấp tim).
3. **Đau ngực / khó thở dữ dội:** đang chạy nhánh `emergency` giống đột quỵ (`chest_pain`, `breathing`).

Từ đối chiếu với bảng mapping tham khảo trong ticket:

4. **Gộp chuyên khoa con:** bảng tham khảo tách Nội tim mạch, Suy tim–Mạch vành, Phẫu thuật tim–Lồng ngực, Hô hấp–Hồi sức tim mạch; DB chỉ có `tim_mach`, `ho_hap`. Đề xuất chấp nhận gộp ở giai đoạn này vì dữ liệu cơ sở không chi tiết hơn.
5. **Chấn thương:** bảng tham khảo xếp trật khớp, bong gân, gãy xương vào Cơ xương khớp; app có riêng `ngoai_chan_thuong`. Đề xuất: có té ngã / va đập / tai nạn → `ngoai_chan_thuong`, đau mạn tính → `co_xuong_khop`.
6. **Bối cảnh sau chấn thương đầu:** thêm cho `headache`, `numbness` (bản nháp mới có ở đau khớp, đau lưng). Chảy máu tai / mũi, lơ mơ sau chấn thương đầu → `emergency` (`head_injury`). Đang chạy như vậy.
7. **Khàn tiếng:** bảng tham khảo xếp vào Phẫu thuật lồng ngực; đề xuất giữ Tai mũi họng như bản nháp.
8. **Chuyên khoa không có thẻ triệu chứng:** `mat`, `rang_ham_mat`, `san`, `ung_buou`, `y_hoc_co_truyen`, `ngoai_chan_thuong` không thẻ nào dẫn tới, nên "đau răng", "mờ mắt" hiện sẽ ra đa khoa. Đề xuất thêm vào bảng map một mục "cụm từ → chuyên khoa" cho các khoa này, Product duyệt danh sách cụm từ. Chưa duyệt thì service giữ luật chặt: ra đa khoa.

## 8. Cần team App / data làm

1. Thêm cột `facilities.audience` (`child` / `adult` / `all`), sửa 43 dòng `is_general`, bỏ `than_kinh` ở 3 BV Tâm thần, theo `facility_data_issues.json`.
2. Sau khi sửa, các BV Nhiệt đới, Lão khoa, Phục hồi chức năng, điểm xét nghiệm MEDLATEC sẽ không còn khoa nào và bị ẩn. Cần quyết: thêm key (vd. `truyen_nhiem` cho Nhiệt đới, đi cùng `fever` ở bảng map) hay chấp nhận ẩn. Viện Pháp y Tâm thần nên `is_active = false`.
3. Đổi nhãn `than_kinh` thành "Nội thần kinh" trong app.
4. Thêm "Tìm cho: Tôi / Trẻ em (nhập tuổi)" và gửi `profile.age` theo lựa chọn đó.
5. Kiểm thêm các dòng đang gắn đa khoa mà tên cho thấy không phải, gặp khi chạy thử trên dữ liệu thật quanh Q1: "Trung Tâm Y Tế Dự Phòng - Phòng Khám Tiêm Phòng" (điểm tiêm phòng), "Bệnh viện Bình Dân" (BV ngoại khoa / tiết niệu).
