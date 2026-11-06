# Gợi ý cơ sở khám theo triệu chứng (`/v1/facility/suggest`) — KAN-115

**Trạng thái (2026-11-05):** team App đã duyệt hợp đồng (mục 2–4) và làm xong mục 8 (gồm cả `age: null`, `locale`, luồng 429). App phát hành 05/11; bản Android 2034 đã bật cờ `FACILITY_AI`, nên đã có lượt gọi thật. Migration `20261102000000_facility_audience_and_fixes.sql` đã chạy trên production tối 03/11; server đã bật `FACILITY_AUDIENCE_COLUMN=true` ngày 05/11, nên DB là nguồn đúng và `facility_overrides.json` không còn dùng. Product còn chốt các câu ở mục 7; chỗ nào chưa chốt thì service đang chạy theo đề xuất ghi ở đó.

**Cập nhật 2026-11-06:** thêm 35 triệu chứng bổ sung ngoài 25 thẻ của app (mục 4.1) và bước đối chiếu nguyên chữ sau khi model đọc câu gõ (mục 4.2), prompt `facility_v2`. Sửa lỗi log production 05/11: "Trầm cảm" bị log `facility_unmapped` và app hiện ghi chú "chưa có trong bảng" dù đã gợi ý đúng Tâm thần – Tâm lý.

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
| 1. Chuẩn hoá triệu chứng | Thẻ app gửi: dùng thẳng. Câu gõ tự do: LLM, chỉ được trả key trong `symptom_tags.json` hoặc `extra_symptoms.json`, rồi code đối chiếu nguyên chữ (mục 4.2) | LLM không được trả key lạ; key lạ bị bỏ |
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
| `profile.age` | Có | Tuổi của **hồ sơ đang chọn** (không phải chủ tài khoản), số nguyên 0–120. `≤ 16` là trẻ em. `null` = người lớn chưa rõ tuổi (tính như người lớn). Không gửi ngày sinh, tên. |
| `symptom_tags` | Một trong hai | Key trong `symptom_tags.json`. Key lạ → `422`. |
| `text` | Một trong hai | Câu gõ tự do, tối đa 500 ký tự. |
| `location` | Có | Toạ độ đã làm tròn. |
| `radius_km` | Không | Mặc định 10, tối đa 50. |
| `limit` | Không | Mặc định 10, tối đa 30. |
| `locale` | Không | Nhận và bỏ qua: hiện `reason`, `message`, `note`, nhãn chuyên khoa luôn bằng tiếng Việt, giống `/v1/chat`. Trường lạ nói chung không gây `422`. |
| `clarify_choice` | Không | Gửi lại khi trả lời câu hỏi làm rõ (mục 3.3): `value` của lựa chọn người dùng bấm, kèm nguyên request cũ. Nhận key chuyên khoa trong `specialties.json` hoặc `general_only`; giá trị khác → `422`. Kết quả đọc câu gõ được cache 24 giờ theo nội dung câu, nên lượt này thường không gọi lại model. Gửi kèm `radius_km` được. |

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
- `symptoms[].key`: thẻ của app, **hoặc** key triệu chứng bổ sung (mục 4.1, vd `toothache`, `menstrual`) khi `source` là `text`. App hiển thị theo `name`, không tra key trong danh sách thẻ của app.
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
- `id` là `facilities.id` (uuid) của chính bảng app đang đồng bộ, để ghép ghim bản đồ và nút chỉ đường.

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
- `facilities`: 3 cơ sở đa khoa gần nhất hợp đối tượng, không giới hạn bán kính. Mỗi cơ sở có đủ trường như ở `ok` (luôn có `id`, `lat`, `lng`), nên app chỉ đường được cả khi máy chưa có cơ sở đó. DB chưa có cột "có cấp cứu", nên đây là gần đúng.

## 4. Lọc và xếp hạng

**Bỏ hẳn** (không bao giờ gợi ý):
- Cơ sở chưa gắn gì: `is_general = false` và `specialties` rỗng (400 dòng).
- Viện Pháp y Tâm thần.

**Sửa tạm** theo [`app/facility_data/facility_overrides.json`](../app/facility_data/facility_overrides.json) (dựng từ `facility_data_issues.json`), **chỉ khi `FACILITY_AUDIENCE_COLUMN=false`**. Bật cờ (sau khi migration của team App chạy) thì DB là nguồn đúng và danh sách này không còn dùng:
- 43 dòng `is_general` sai: bỏ cờ đa khoa, gán khoa suy từ tên (Nhi TW → `nhi`, Mắt TW → `mat`, Tâm thần TW → `tam_than`, Nhiệt đới → `truyen_nhiem`…). Lão khoa, Phục hồi chức năng, MEDLATEC không có key nên bị ẩn.
- 3 BV Tâm thần: bỏ tag `than_kinh`.
- 17 cơ sở chỉ khám trẻ em: `audience = child`.

Thêm vài luật theo tên làm lưới an toàn cho dòng chưa có trong danh sách trên. Luật nào cũng chỉ bớt chỗ cơ sở có thể xuất hiện, không bao giờ thêm, và vẫn chạy sau khi bật cờ (trừ luật đối tượng, xem dưới):
- tên có "tâm thần" → bỏ `than_kinh`, thêm `tam_than`, bỏ cờ đa khoa;
- tên có "xét nghiệm", "tiêm phòng" hoặc "dự phòng" → bỏ cờ đa khoa;
- chỉ khi cờ tắt: tên có "nhi" hoặc "trẻ em" mà không có "sản" → `audience = child` (so khớp có dấu, nên "Nhị" không bị tính). Bật cờ thì dùng `audience` của DB;
- tên có "pháp y" → ẩn.

**Lọc cứng**, giữ cơ sở thoả **đồng thời**:
1. Có chuyên khoa đã map (`specialties` giao với `primary ∪ secondary`), **hoặc** là đa khoa (`is_general`), **hoặc** (chỉ với trẻ em) là cơ sở Nhi.
2. Đối tượng khớp: người lớn → bỏ `audience = child`; trẻ em → bỏ `audience = adult`. Chưa có cột thì coi là `all`, trừ các dòng sửa tạm và luật theo tên ở trên.
3. Trong `radius_km`, tính bằng haversine từ `lat/lng`.

**Xếp hạng**:
- Người lớn: `primary` → `secondary` → `general`; cùng nhóm thì gần hơn trước.
- Triệu chứng mà bảng map trỏ thẳng tới đa khoa (`primary: ["da_khoa"]`, vd. sốt ở người lớn) thì cơ sở đa khoa đứng **cùng hạng** chuyên khoa chính, với `reason` dạng "Sốt → Đa khoa". Ví dụ sốt ở người lớn: đa khoa gần trước, BV Nhiệt đới (`truyen_nhiem`, phụ) sau.
- Trẻ em: cơ sở Nhi (`audience = child` hoặc có tag `nhi`) có đúng khoa → khoa đúng ở nơi khác → cơ sở Nhi bất kỳ → đa khoa; cùng nhóm thì gần hơn trước.

**Bối cảnh trong câu gõ** (LLM đọc, code áp dụng):
- Sau té ngã / va đập / tai nạn: `joint_pain`, `back_pain`, `headache`, `numbness` → `ngoai_chan_thuong` chính, chuyên khoa gốc lùi xuống phụ.
- Người dùng nói rõ vấn đề tinh thần → thêm `tam_than` chính.

**Triệu chứng chưa có trong bảng map**: gợi ý đa khoa, ghi `note`, và ghi log cụm từ chưa map (`facility_unmapped phrase=…`, không kèm user id, không kèm toạ độ) để team bổ sung bảng. Không map theo chữ gần giống.

### 4.1 Triệu chứng bổ sung (`extra_symptoms.json`)

35 mục ngoài 25 thẻ của app, dựng từ bảng mapping tham khảo trong ticket (Google Doc), mỗi mục có tên, từ đồng nghĩa và chuyên khoa chính / phụ cho người lớn và trẻ em. Chỉ dùng các key chuyên khoa DB đang có; nhóm nào DB chưa có khoa riêng thì về đa khoa. **Đề xuất phía AI, cần Product duyệt** (thay cho mục 7.8).

| Nhóm | Mục | Chuyên khoa (người lớn) |
|---|---|---|
| Răng miệng | Đau răng, Chảy máu chân răng, Nhiệt miệng | Răng Hàm Mặt (nhiệt miệng: Tai Mũi Họng, phụ RHM) |
| Mắt | Mờ mắt, Đau / đỏ mắt | Mắt |
| Tai mũi họng | Đau / ù tai, Chảy máu cam, Ngủ ngáy, Viêm xoang | Tai Mũi Họng |
| Tiêu hoá | Ợ chua / trào ngược, Đi ngoài ra máu, Vàng da | Tiêu hoá (ra máu: phụ Ngoại tổng quát) |
| Ngoại | Trĩ / đau hậu môn, Thoát vị / sưng bẹn, Nghi sỏi thận | Ngoại tổng quát |
| Tiết niệu, nam khoa | Tiểu buốt / tiểu rắt, Vấn đề nam khoa | Đa khoa, phụ Ngoại tổng quát (DB chưa có Tiết niệu, Nam khoa) |
| Sản phụ khoa | Rối loạn kinh nguyệt, Mang thai | Sản – Phụ khoa |
| Ung bướu | Khối u / nổi hạch | Ung bướu, phụ Ngoại tổng quát |
| Nội tiết | Bướu cổ / tuyến giáp, Khát nước / tiểu nhiều | Nội tiết |
| Tim mạch | Huyết áp cao / mỡ máu, Phù chân / phù mặt | Tim mạch (phù: cùng hạng đa khoa) |
| Cơ xương khớp, chấn thương | Đau cổ vai gáy; Bong gân / trật khớp / gãy xương, Bỏng | Cơ xương khớp; Ngoại chấn thương |
| Da | Mụn, nấm da, chàm | Da liễu |
| Thần kinh, tâm lý | Co giật / động kinh; Vấn đề tâm lý, Chậm nói / tăng động | Nội thần kinh; Tâm thần – Tâm lý |
| Khác | Thiếu máu, Khám tổng quát; Nghi sốt xuất huyết; Châm cứu / phục hồi | Đa khoa; Truyền nhiễm – Nhiệt đới; Y học cổ truyền |

Trẻ em: phần lớn giống người lớn; mục mà người lớn về đa khoa thì trẻ em về Nhi.

### 4.2 Đối chiếu nguyên chữ sau khi model đọc

Model nhỏ có lúc đặt đúng từ đồng nghĩa vào `unmapped` ("sổ mũi", "ói"), chép cụm đã xử lý bằng cờ vào `unmapped` ("Trầm cảm" khi đã bật `mental_health`), hoặc hiểu sai câu không dấu ("dau da day" thành đau đầu). Sau bước đọc, code đối chiếu câu gõ với tên và từ đồng nghĩa trong cả hai bảng:

- Cụm khớp trọn tên / từ đồng nghĩa mà model bỏ sót → thêm triệu chứng đó.
- Căn cứ (`matched_text`) của model khớp trọn cụm của triệu chứng khác → tin bảng. Căn cứ là cách nói không có trong bảng → giữ lựa chọn của model.
- `unmapped` bỏ cụm có trong bảng, cụm đã nằm trong một triệu chứng đã nhận, và cụm "thần kinh" khi đã hỏi lại (`ambiguous_neuro`).
- Ưu tiên cụm dài nhất tại mỗi vị trí ("đau đầu gối" là đau khớp). Bỏ cụm đứng sau từ phủ định trong cùng vế câu ("không sốt nhưng ho" vẫn nhận ho).
- Câu có dấu thì khớp cả dấu ("bị tiêu chảy" không phải "bí tiểu"). Câu không dấu thì không dùng cụm một từ và cụm trùng nhau khi bỏ dấu ("kho tieu", "do mat"); những câu này để model đọc.

Đo 2026-11-06 với `gpt-5-nano`, mỗi câu 2 lần. Trước sửa (30 câu, 60 lượt): 5 lượt mất từ đồng nghĩa có sẵn hoặc hiểu sai ("sổ mũi", "ói", "dau da day", "bong gân"), 16 lượt ra đa khoa vì cụm chưa có trong bảng (đau răng, mờ mắt, trễ kinh, tiểu buốt, ù tai, đau cổ vai gáy, ợ chua). Sau sửa (37 câu, 74 lượt, thêm "Trầm cảm", "lo âu", câu phủ định): không còn lượt nào như vậy; `unmapped` chỉ còn "đau gót chân" (1/2 lượt), đúng là cụm chưa có trong bảng.

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

- **Không trừ lượt chat.** Giới hạn riêng `FACILITY_MAX_PER_DAY` (mặc định 30) mỗi ngày mỗi user, **chỉ tính những lần đọc câu gõ bằng model**. Lượt chỉ có thẻ, lượt gửi lại cùng câu (nới bán kính, trả lời câu hỏi làm rõ, dùng kết quả đã cache) và lượt model lỗi đều không tính. Hết lượt → `429 rate_limited`; app vẫn tìm được bằng thẻ.
- **Thời gian:** bước gọi model có trần `FACILITY_LLM_BUDGET_SECONDS` (mặc định 15 giây, gồm cả retry). Quá trần thì gợi ý theo thẻ / đa khoa kèm `note`, từ khoá cấp cứu vẫn chạy. Đo 2026-11-02 với `gpt-5-nano` (12 câu, máy dev): trung vị 2,5 giây, chậm nhất 4,7 giây. Lượt chỉ có thẻ không gọi model, dưới 1 giây khi bảng cơ sở đã cache. App chờ 30 giây là đủ.
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
8. **Chuyên khoa không có thẻ triệu chứng:** *(2026-11-06: đã làm theo hướng này ở mục 4.1, chờ Product duyệt danh sách)* `mat`, `rang_ham_mat`, `san`, `ung_buou`, `y_hoc_co_truyen`, `ngoai_chan_thuong` không thẻ nào dẫn tới, nên "đau răng", "mờ mắt" hiện sẽ ra đa khoa. Đề xuất thêm vào bảng map một mục "cụm từ → chuyên khoa" cho các khoa này, Product duyệt danh sách cụm từ. Chưa duyệt thì service giữ luật chặt: ra đa khoa.

## 8. Cần team App / data làm

Team App đã làm xong (2026-11-02):

1. Migration `20261102000000_facility_audience_and_fixes.sql`: cột `audience` (mặc định `all`), 17 cơ sở `child`, 4 BV Lão khoa `adult`, sửa 43 dòng `is_general`, bỏ `than_kinh` ở 3 BV Tâm thần. **Đã chạy trên production tối 03/11** (Hiếu kiểm lại: 17 cơ sở `child`, 4 BV Lão khoa `adult`).
2. Key mới `truyen_nhiem` ("Truyền nhiễm – Nhiệt đới") cho 6 BV Nhiệt đới / Truyền nhiễm; bên AI đã thêm làm chuyên khoa phụ của `fever` ở người lớn. Chấp nhận ẩn: Lão khoa, Phục hồi chức năng, Điều dưỡng, điểm xét nghiệm, điểm tiêm phòng, y tế dự phòng, BV Thận Hà Nội, Viện Huyết học. Viện Pháp y Tâm thần `is_active = false`.
3. Nhãn `than_kinh` là "Nội thần kinh" trong app.
4. "Tìm cho: Tôi / Trẻ em": "Tôi" gửi tuổi từ hồ sơ, "Trẻ em" nhập 0–16, không lưu.
5. Điểm tiêm phòng ẩn; BV Bình Dân `is_general=false`, `specialties=['ngoai_tong_quat']`.

Team App xác nhận thêm (2026-11-03, nhánh develop, chưa phát hành, vẫn sau cờ `FACILITY_AI`):

6. Hồ sơ chưa có tuổi → gửi `profile.age: null` (không gửi 30 nữa). Service tính là người lớn.
7. Gửi `locale` (`vi` / `en`). Giao diện tiếng Anh hiện một dòng báo lời giải thích chỉ có tiếng Việt. Service nhận và bỏ qua `locale`.
8. Gặp `429 rate_limited` ở lượt có câu gõ: đã chọn thẻ → nút "Tìm bằng thẻ đã chọn" gửi lại **không có `text`** (lượt này không gọi model nên không bị 429); chưa chọn thẻ → nút "Chọn thẻ triệu chứng" xoá câu gõ, về khung nhập. Lượt chỉ có thẻ mà vẫn 429 (chỉ có thể là rate limit của nginx) thì app chỉ báo hết lượt.
9. Thời gian chờ 30 giây. `emergency`, `radius_km` kèm `clarify_choice`, `id` giữ nguyên như hợp đồng.

App phát hành 05/11 trên cả hai store; bản Android 2034 bật `FACILITY_AI`. KAN-118 (phần việc phía App) đã Done.

Phía AI đã đặt `FACILITY_AUDIENCE_COLUMN=true` trên server và deploy lại (05/11).
