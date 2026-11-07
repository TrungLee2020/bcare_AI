# KAN-134: disclaimer cố định, không khẳng định tuyệt đối, nhận diện ngoài danh mục — thiết kế

Ngày: 2026-11-07. Trạng thái: đã duyệt hướng, chờ duyệt spec.

Ticket: KAN-134 (Tristan). Liên quan: KAN-115 (gợi ý cơ sở khám, `docs/facility-suggest.md`), KAN-132 (tài liệu y tế chính thống, đang Backlog, chưa có file).

## 1. Mục tiêu và phạm vi

| Hạng mục ticket | Làm lần này |
|---|---|
| (a) Disclaimer cố định dưới mỗi câu trả lời | API trả trường `disclaimer`; App hiển thị. Cờ để Bụt thôi tự viết câu đó khi App đã hiển thị |
| (b) Không khẳng định tuyệt đối khi tư vấn y tế / thuốc | Prompt `system_v8` + đo cụm tuyệt đối (không chặn) + bộ test thật ≥ 10 câu |
| (c) Nhận diện triệu chứng / chuyên khoa ngoài danh mục cố định | Luồng gợi ý cơ sở: cụm ngoài bảng → model giả định chuyên khoa, gắn nhãn, luôn kèm đa khoa, ghi log để Product duyệt |
| (c) "Train bằng dữ liệu KAN-132" | **Chưa làm**: KAN-132 chưa có tài liệu. Không fine-tune model; khi có tài liệu thì bổ sung vào bảng tham khảo / prompt và ghi nguồn |

Ngoài phạm vi: RAG / kho tài liệu; tự động đưa giả định vào bảng (Product duyệt tay); OCR (spec riêng `2026-11-06-chat-image-ocr-design.md`); chèn câu cấp cứu bằng code.

## 2. (c) "Bụt giả định" chuyên khoa cho cụm ngoài bảng

Nguyên tắc KAN-115 giữ nguyên cho phần có trong bảng: bảng đã duyệt (`symptom_specialty_map.json`, `extra_symptoms.json`) luôn được ưu tiên. Phần mới chỉ áp dụng cho cụm model xếp vào `unmapped`.

### 2.1 Đọc câu gõ (prompt `facility_v3`)

- Schema: `unmapped` đổi từ `list[str]` thành `list[{text, specialty, reason}]`:
  - `text`: đoạn chữ nguyên văn (như cũ, phải có trong câu gõ — `_grounded`).
  - `specialty`: một mã trong `specialties.json` trừ `tam_than` và `nhi`, hoặc `""` khi không đủ chắc.
  - `reason`: ≤ 80 ký tự, tiếng Việt, nêu triệu chứng dẫn tới chuyên khoa (vd "ù tai kéo dài là bệnh tai").
- Prompt đưa kèm bảng 19 chuyên khoa (key, nhãn, triệu chứng điển hình — từ `specialties.json`, nguồn: bảng tham khảo trong KAN-115) làm căn cứ. Quy tắc: chỉ chọn khi triệu chứng thuộc rõ một chuyên khoa; không chọn theo chữ giống nhau; không chọn Tâm thần (chỉ qua cờ `mental_health`) hay Nhi (đã lọc theo tuổi); không chắc thì `""`.
- Đổi version prompt → cache đọc câu gõ 24 giờ tự làm mới.
- Code kiểm lại: `specialty` không thuộc danh sách cho phép → coi như `""`. `reason` cắt 80 ký tự.

### 2.2 Map và xếp hạng

- `Interpretation` thêm `guesses: list[(text, specialty, reason)]` (chỉ mục có `specialty`); `unmapped` vẫn là danh sách chữ như cũ (mọi cụm ngoài bảng, kể cả có giả định).
- `catalog.map_symptoms` nhận thêm `guesses`: mỗi giả định thành `SpecialtyPick(key, role="secondary", sources=["assumed"], assumed=True, reason=…)`. Chuyên khoa đã có từ bảng thì giữ vai của bảng, không gắn `assumed`.
- Xếp hạng (`ranking.py`): cơ sở chỉ khớp chuyên khoa giả định xếp **ngang hạng đa khoa** (`general`), cùng hạng thì gần trước. Khớp từ bảng (primary / secondary) vẫn đứng trên.
- `reason` của cơ sở: "Bụt giả định: {reason} → {nhãn chuyên khoa} · Phù hợp: …".

### 2.3 Response (chỉ thêm trường, không đổi trường cũ)

- `interpretation.specialties[]`: mục giả định có `role: "secondary"`, `from: ["assumed"]`, thêm `assumed: true`, `reason`. Mục từ bảng có `assumed: false`.
- `interpretation.note` khi có giả định: "Một phần mô tả chưa có trong bảng; Bụt tạm đoán chuyên khoa {nhãn}, kèm cơ sở đa khoa để bạn chọn."
- `facilities[].match`: không thêm giá trị mới. Cơ sở khớp chuyên khoa giả định có `match: "secondary"` (App đã hiểu giá trị này) và `matched_specialties` chứa mã giả định; vị trí trong mảng là ngang hạng đa khoa (mục 2.2), App hiển thị đúng thứ tự mảng như hiện nay.

### 2.4 Log để mở rộng bảng

`facility_unmapped phrase=%r guess=%s group=%s` (guess = mã hoặc `none`), không user id, không toạ độ. Metric `facility_guess_<mã>`, `facility_guess_none`. Product xem log định kỳ; cụm đúng → thêm vào `extra_symptoms.json` (thành mapping đã duyệt).

## 3. (a) Disclaimer cố định

- Cấu hình: `CHAT_DISCLAIMER` (mặc định "Thông tin mang tính tham khảo, không thay thế khám chữa bệnh."), `CHAT_DISCLAIMER_IN_APP` (mặc định `false`).
- `POST /v1/chat`: JSON body và event `done` luôn có `"disclaimer": <CHAT_DISCLAIMER>`, cho mọi đường trả lời (model, câu chào, khủng hoảng / cấp cứu, ảnh không kèm câu hỏi).
- `GET /v1/conversations/{id}/messages`: tin `assistant` có `disclaimer`.
- `CHAT_DISCLAIMER_IN_APP=true` (bật sau khi App phát hành bản hiển thị):
  - Prompt: dòng quy tắc "Luôn nhắc đây là thông tin tham khảo…" mang dấu `<!-- app-disclaimer: drop -->`; loader bỏ các dòng có dấu này khi cờ bật (cache theo cả cờ). Dấu bị gỡ trước khi gửi model như `leak-check`.
  - Hậu xử lý: xoá câu trong `answer` khớp mẫu disclaimer (dạng bỏ dấu `thong tin ... tham khao ... khong thay the`), chỉ khi câu đó đứng riêng; còn lại để nguyên.
- Không đổi `/v1/report/monthly`, `/v1/facility/suggest`.

## 4. (b) Không khẳng định tuyệt đối

### 4.1 Prompt `system_v8`

`system_v8.md` = `system_v7.md` + mục mới "Nói theo điều kiện, không khẳng định tuyệt đối" (áp dụng mọi câu hỏi y tế):
- Dùng "thường", "với đa số người", "nếu …"; nêu khi nào lời khuyên không áp dụng (bệnh nền, đang dùng thuốc, mang thai, trẻ nhỏ…).
- Không mở đầu bằng "được, không sao cả", "chắc chắn", "hoàn toàn an toàn"; câu đầu là hướng dẫn kèm điều kiện.
- Thêm một câu trả lời mẫu (vitamin C buổi tối) viết theo cách này, trong khối `leak-check: off`.
- Dòng disclaimer mang dấu `<!-- app-disclaimer: drop -->` (mục 3).
- `PROMPT_VERSION` mặc định → `v8`. v7 chưa deploy production nên không giữ v7 làm bản chạy.

### 4.2 Đo cụm tuyệt đối (không chặn)

`output_validator.absolute_claims(text) -> list[str]` trả tên các mẫu khớp trên chữ đã bỏ dấu: `khong_sao_ca`, `chac_chan`, `hoan_toan_an_toan`, `hoan_toan_binh_thuong`, `tuyet_doi`, `100_phan_tram`, `dam_bao_khoi`. Gọi sau khi validate thành công: có khớp thì `metrics.incr("answer_absolute_claim")`, `metrics.incr("answer_absolute_claim_<tên>")`, log `request_id` + tên mẫu (không log câu trả lời). Không chặn, không sửa câu.

### 4.3 Kiểm thử thật

`tests/test_live_openai.py` thêm bộ ≥ 10 câu: vitamin C buổi tối, sắt với sữa, thuốc cảm + paracetamol, dầu cá + thuốc chống đông, cà phê khi cao huyết áp, ăn trứng mỗi ngày, nhịn ăn sáng, ngủ trưa lâu, uống nhiều nước, đau lưng tập gì. Kiểm: trạng thái `ok`, không `out_of_scope`, câu đầu không có cụm tuyệt đối (`absolute_claims` trên câu đầu rỗng). Kết quả hai lần chạy ghi vào KAN-134.

## 5. Kiểm thử đơn vị

- (c) schema `unmapped` mới strict-valid; `specialty` ngoài danh sách cho phép / `tam_than` / `nhi` bị bỏ; `guesses` vào mapping với `assumed`; chuyên khoa vừa có từ bảng vừa giả định giữ vai của bảng; xếp hạng ngang đa khoa; response có `assumed`, `reason`, `note`; log `guess=`.
- (a) `disclaimer` có ở JSON, SSE `done`, câu chào, khủng hoảng, ảnh không câu hỏi, lịch sử; cờ bật → dòng có dấu bị bỏ khỏi prompt, câu disclaimer bị xoá khỏi `answer`, câu khác giữ nguyên; cờ tắt → không đổi.
- (b) `absolute_claims` khớp / không khớp (có dấu, không dấu, phủ định "không chắc chắn" vẫn tính — chấp nhận, chỉ để đo); metric tăng, câu trả lời không đổi; v8 không bị coi là lộ prompt với câu trả lời mẫu.

## 6. Việc cần người khác

- Team App (@ Hiếu): hiển thị `disclaimer` cố định dưới mỗi tin của Bụt, không bị che khi cuộn; báo bản phát hành để bên AI bật `CHAT_DISCLAIMER_IN_APP`. App hiển thị mục chuyên khoa có `assumed: true` kèm nhãn "Bụt giả định" và `reason`.
- Product (Tristan): duyệt log giả định định kỳ; gửi tài liệu KAN-132 khi có.

## 7. Kết quả đo (2026-11-07, OpenAI thật, `PROMPT_VERSION=v8`, `FACILITY_PROMPT_VERSION=v3`)

**Chat, 10 câu "không khẳng định tuyệt đối ở câu đầu"** (`tests/test_live_openai.py -k khong_khang_dinh`): lần 1 xanh 10/10, lần 2 xanh 10/10, không phải sửa `system_v8`. Ví dụ câu đầu:
- "Mẹ tôi đang uống thuốc chống đông, uống thêm dầu cá được không?" → "Dầu cá có thể làm tăng nguy cơ chảy máu khi dùng cùng thuốc chống đông, nên bạn không nên tự cho mẹ uống thêm."
- "Uống vitamin C vào buổi tối có được không?" → "Thông thường, uống vitamin C vào buổi tối không ảnh hưởng đến giấc ngủ của đa số mọi người."

**Giả định chuyên khoa, 3 câu** (`tests/test_facility_live.py`):
- `facility_v3` bản đầu: lần 1 đỏ 0/3, lần 2 đỏ 0/3. Model gán cụm vào thẻ có sẵn (`joint_pain`, `blurry_vision`, `skin_problem` / `hemorrhoid`) hoặc để `specialty` rỗng nên không có giả định.
- Sau vòng sửa prompt 1 (chỉ chọn thẻ khi đúng chính triệu chứng đó): 1/3 và 0/3.
- Sau vòng sửa prompt 2 (chọn chuyên khoa gần nhất khi vùng cơ thể đã rõ, "đau gót chân" không phải "đau khớp"): lần A 3/3, lần B 2/3. **Còn đỏ không ổn định:** "đau gót chân mỗi sáng khi mới bước xuống giường" đôi khi vẫn được model gán vào `joint_pain` (không vào `unmapped`), nên không có giả định. Ví dụ giả định xanh: "nhìn một thành hai" → `mat`, lý do "nhìn đôi có thể liên quan mắt".
- Ghi chú: trường hợp trên vẫn ra chuyên khoa đúng (`joint_pain` → cơ xương khớp), chỉ là không gắn nhãn "Bụt giả định". Test không được nới.

**Đo lại giả định chuyên khoa (fix round 1):** test live chấm theo kết quả, không theo đường đi. Test dựng mapping cuối như service (`catalog.map_symptoms` với triệu chứng, cờ và `guesses`), rồi kiểm: (1) tập chuyên khoa đã map giao với tập chấp nhận của câu (đến đúng chuyên khoa bằng bảng hoặc bằng giả định đều tính; bảng thắng giả định là chủ đích); (2) mọi mã giả định nằm trong tập chấp nhận; (3) không có `tam_than` / `nhi` ở mục giả định. Ba câu và tập chấp nhận giữ nguyên, không sửa prompt thêm. Kết quả: lần 1 xanh 3/3, lần 2 xanh 3/3 (câu đau gót chân lần 1 đến `co_xuong_khop` qua thẻ `joint_pain`, lần 2 qua giả định).
