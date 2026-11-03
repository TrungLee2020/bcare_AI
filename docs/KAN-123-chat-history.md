# Lịch sử trò chuyện với Bụt: trả lời team App và câu hỏi cho KAN-123

Từ team AI, 2026-11-03.

- **Phần A** trả lời team App về màn Lịch sử trò chuyện. Đặc tả đầy đủ: mục "Lịch sử chat" trong [`docs/v1-app-api.md`](v1-app-api.md) (repo bcare_AI).
- **Phần B** là các câu hỏi cần pháp chế trả lời cho hồ sơ KAN-123, kèm thông tin dữ liệu phía AI.

> Phần pháp lý dưới đây do team kỹ thuật tự tìm hiểu, dựa trên Luật Bảo vệ dữ liệu cá nhân 2025 (Luật 91/2025/QH15, gọi tắt là Luật BVDLCN) và Nghị định 356/2025/NĐ-CP, chủ yếu qua các bản tóm tắt. Đây không phải ý kiến pháp lý, pháp chế cần đối chiếu với văn bản gốc.

---

## Phần A. Trả lời team App

Cảm ơn team đã làm xong màn Lịch sử trò chuyện. Team AI đã đối chiếu với hợp đồng API và có mấy điểm sau.

### A1. Cần sửa: ngữ cảnh gửi kèm `POST /v1/chat`

`POST /v1/chat` nhận tối đa **48 message**. Nếu app gửi toàn bộ tin đã tải (50 tin mỗi trang), hội thoại từ 24 lượt trở lên sẽ nhận `422 invalid_request`.

Nhờ team chỉ gửi **tối đa 47 tin gần nhất kèm câu hỏi mới**. Service chỉ dùng câu hỏi cuối và tự nạp ngữ cảnh theo `conversation_id`, nên gửi ít hơn không làm câu trả lời kém đi.

### A2. Phân trang

Hết trang thì service trả `next_cursor: null`. Service không bao giờ trả lại đúng cursor vừa nhận. App giữ phần kiểm tra cursor trùng làm chốt an toàn cũng được, nhưng điều kiện dừng chính là `null`.

### A3. `ai_chat_version` = `2026-11-03`

Team AI đã kiểm tra cấu hình server: service chỉ cần giá trị khác null. Tăng version không ảnh hưởng gì.

### A4. Thời hạn lưu hội thoại

Lịch sử được lưu **cho tới khi người dùng xoá**, từng hội thoại hoặc toàn bộ khi xoá tài khoản. Câu trên màn đồng ý xem đề xuất ở mục B3; câu chính thức chờ pháp chế duyệt.

### A5. API xoá (đã chạy trên `https://ai.but.care`)

**`DELETE /v1/conversations/{conversation_id}`**: xoá một hội thoại. Dùng được cho thao tác vuốt để xoá.
- Thành công trả `204`, không có body.
- Hội thoại không có, đã xoá, hoặc của tài khoản khác trả `404 not_found`.
- Hội thoại đã xoá không dùng lại được. Hỏi tiếp bằng `conversation_id` đó thì Bụt không nhớ gì và lượt mới không được lưu, nên app mở hội thoại mới.

**`DELETE /v1/conversations`**: xoá **toàn bộ** lịch sử của tài khoản.
- Luôn trả `204`, gọi lại nhiều lần vẫn an toàn.
- **Khi xoá tài khoản:** app gọi API này **trước khi** xoá tài khoản Supabase, vì xoá tài khoản xong thì token không dùng được nữa. Nếu gọi lỗi (`503`, mất mạng) thì chưa xoá tài khoản, và cho người dùng thử lại.

Cả hai API chỉ cần `Authorization`, không cần người dùng đã đồng ý dùng AI. Lỗi có cùng dạng `{"error", "message"}` như các API khác. Xử lý `401` / `503` như màn lịch sử.

**Sau khi xoá:** hội thoại biến mất ngay khỏi lịch sử và khỏi ngữ cảnh của Bụt. Service xoá hẳn khỏi cơ sở dữ liệu sau 14 ngày (chi tiết ở B4).

### A6. Chờ pháp chế

Câu trên màn đồng ý (B3) và việc xử lý lịch sử cũ (B1) có thể làm thay đổi hành vi của app trước khi phát hành. Team AI sẽ báo lại khi KAN-123 có kết luận.

---

## Phần B. Câu hỏi cho pháp chế (KAN-123)

### B1. Lịch sử đã lưu trong khi câu đồng ý nói "không lưu"

**Hiện trạng**
- Câu đồng ý hiện tại của app nói: *"cuộc trò chuyện không được lưu, đóng app là mất"*.
- Thực tế, từ khi Trợ lý AI chạy trên production, server **đã lưu** các lượt hỏi-đáp thành công. Server còn lưu bản tóm tắt hội thoại do AI viết, để Bụt nhớ mạch trò chuyện.
- Phạm vi hiện còn nhỏ: tính năng mới mở cho một nhóm tài khoản (rollout). Số liệu: **[team AI bổ sung: số tài khoản / số hội thoại / ngày lưu đầu tiên]**.
- Bản app mới đổi câu đồng ý thành "được lưu trên máy chủ của Bụt để bạn xem lại", và hỏi đồng ý lại một lần.

**Quy định liên quan**
- Điều 9 khoản 2 Luật BVDLCN: sự đồng ý chỉ có hiệu lực khi người dùng "biết rõ loại dữ liệu và mục đích xử lý".
- Điều 9 khoản 4: đồng ý theo từng mục đích.
- Không được xử lý vượt mục đích đã công bố hoặc đã thoả thuận với người dùng.
- Dữ liệu sức khoẻ là dữ liệu nhạy cảm (Điều 4 Nghị định 356/2025).

**Các phương án**

| | Phương án | Ưu | Nhược |
|---|---|---|---|
| **1** | **Xoá toàn bộ lịch sử lưu trước ngày phát hành bản app mới** *(team AI đề xuất)* | Gọn nhất về pháp lý; ít dữ liệu, ít người dùng | Người dùng nhóm rollout mất các cuộc trò chuyện cũ |
| 2 | Giữ lại; câu đồng ý mới nói rõ áp dụng cả cho các cuộc trò chuyện trước đây; ai không đồng ý thì xoá dữ liệu của người đó | Người đồng ý giữ được lịch sử | Không hợp thức hoá được việc đã lưu trước đó |
| 3 | Giữ nguyên, không làm gì | — | Không khuyến nghị |

**Khoảng trống tới ngày phát hành:** người dùng đang dùng bản app cũ vẫn đồng ý với câu "không lưu", nhưng server vẫn tiếp tục lưu. Nếu cần chặn ngay, team AI làm được trong khoảng 1 ngày như sau:
- Chỉ lưu lịch sử cho người đã đồng ý bản `2026-11-03`.
- Với người còn bản cũ, Bụt vẫn nhớ mạch trong lúc trò chuyện, nhưng nội dung không hiện ở màn lịch sử và tự xoá sau 1 ngày.

**Cần pháp chế trả lời**
1. Chọn phương án nào: 1, 2, hay phương án khác?
2. Có cần chặn ngay khoảng trống trước ngày phát hành như mô tả ở trên không?
3. Có phải thông báo cho người dùng nhóm rollout về việc lịch sử đã được lưu không?

### B2. Gửi dữ liệu sang OpenAI (Mỹ): hồ sơ chuyển dữ liệu ra nước ngoài

**Hiện trạng.** Bụt dùng model của OpenAI (đặt tại Mỹ). Mỗi lượt hỏi, server gửi sang OpenAI:
- Câu hỏi, cộng tóm tắt và các lượt gần nhất của cuộc trò chuyện.
- Hồ sơ sức khoẻ, nếu người dùng bật chia sẻ hồ sơ.
- Số liệu báo cáo sức khoẻ tháng (Bụt tóm tắt).
- Mô tả triệu chứng khi tìm nơi khám.

Server **không** gửi tên, số điện thoại hay mã người dùng. Nhưng người dùng có thể tự gõ những thông tin đó vào câu hỏi.

Về phía OpenAI:
- OpenAI không dùng dữ liệu gửi qua API để huấn luyện model.
- Theo mặc định, OpenAI giữ log giám sát lạm dụng **tới 30 ngày**.
- Muốn OpenAI không giữ gì (Zero Data Retention) phải được OpenAI duyệt riêng.

**Quy định liên quan**
- Điều 20 khoản 1 điểm c Luật BVDLCN: dùng nền tảng ở nước ngoài để xử lý dữ liệu thu thập tại Việt Nam được coi là **chuyển dữ liệu xuyên biên giới**.
- Điều 20 khoản 2: lập hồ sơ đánh giá tác động chuyển dữ liệu, gửi bản chính cho cơ quan chuyên trách (Cục A05, Bộ Công an) **trong 60 ngày kể từ ngày chuyển đầu tiên**. Theo các nguồn tư vấn: hồ sơ theo Mẫu 09 kèm thông báo Mẫu 01a của Nghị định 356, gồm báo cáo đánh giá, hợp đồng với bên nhận và chính sách bảo vệ dữ liệu.
- Điều 21 khoản 1: hồ sơ **đánh giá tác động xử lý dữ liệu**, cùng hạn 60 ngày kể từ ngày đầu tiên xử lý.
- Doanh nghiệp nhỏ và khởi nghiệp được miễn đánh giá tác động 5 năm. Nhưng theo các nguồn tư vấn, miễn trừ này **không áp dụng** khi trực tiếp xử lý dữ liệu nhạy cảm, mà dữ liệu sức khoẻ thuộc nhóm nhạy cảm.
- Mức phạt tối đa cho vi phạm về chuyển dữ liệu xuyên biên giới: 5% doanh thu năm trước liền kề (Điều 8 Luật BVDLCN).

**Mốc thời gian (gấp).** Cần xác định ngày đầu tiên dữ liệu người dùng thật được gửi sang OpenAI:
- Service lên server: 29/09/2026 → hạn 60 ngày là **28/11/2026**.
- Trợ lý AI chạy cho app (`ai.but.care/v1`): 14/10/2026 → hạn **13/12/2026**.

**Cần pháp chế trả lời**
1. Đã lập và nộp hồ sơ chuyển dữ liệu ra nước ngoài (Điều 20) và hồ sơ đánh giá tác động xử lý dữ liệu (Điều 21) chưa? Nếu chưa, ai lập, và ngày chuyển đầu tiên chốt là ngày nào?
2. Pháp nhân nào là bên kiểm soát dữ liệu (ghi trên màn đồng ý và trong hồ sơ)?
3. Có ký thoả thuận xử lý dữ liệu (DPA) với OpenAI không? Có cần xin Zero Data Retention không?
4. OpenAI giữ log tới 30 ngày, dài hơn thời hạn khoảng 20 ngày để xoá theo yêu cầu (mục B4). Pháp chế đánh giá thế nào? Có cần ghi điều này vào chính sách không?
5. Câu đồng ý có phải nêu việc gửi dữ liệu sang OpenAI tại Mỹ không? (Đề xuất ở B3 có câu này.)

### B3. Câu chữ màn đồng ý: đề xuất để pháp chế duyệt

Điều 9 khoản 2 yêu cầu người dùng biết rõ loại dữ liệu, mục đích, bên kiểm soát, quyền và nghĩa vụ của mình. Đề xuất bổ sung cho câu mới của app:

> Cuộc trò chuyện với Bụt (câu hỏi, câu trả lời và bản tóm tắt cuộc trò chuyện) được lưu trên máy chủ của Bụt để bạn xem lại và để Bụt nhớ mạch trò chuyện, **cho tới khi bạn xoá cuộc trò chuyện hoặc xoá tài khoản**. Để trả lời, nội dung trò chuyện được gửi tới nhà cung cấp AI là OpenAI (Hoa Kỳ); OpenAI không dùng dữ liệu này để huấn luyện mô hình. Bạn có thể xem lại hoặc xoá lịch sử bất cứ lúc nào trong ứng dụng.

**Cần pháp chế trả lời:** duyệt hoặc sửa câu trên. Tên bên kiểm soát dữ liệu ghi ở đâu (màn đồng ý hay chính sách)?

### B4. Thông tin dữ liệu phía AI (để đưa vào chính sách)

**Thời hạn lưu:** lịch sử trò chuyện được lưu cho tới khi người dùng xoá. Luật không đặt thời hạn cố định; Điều 3 khoản 3 chỉ yêu cầu lưu "phù hợp với mục đích xử lý". Mục đích ở đây là để người dùng xem lại và để Bụt nhớ mạch, và người dùng tự xoá được bất cứ lúc nào.

**Khi người dùng xoá** (từng hội thoại, hoặc toàn bộ khi xoá tài khoản):
- Hội thoại biến mất ngay khỏi lịch sử và khỏi ngữ cảnh của Bụt.
- **Sau 14 ngày** được xoá hẳn khỏi cơ sở dữ liệu, gồm cả bản tóm tắt.
- Theo Nghị định 356, yêu cầu xoá phải xong trong khoảng 20 ngày.

**Dữ liệu ở đâu, giữ bao lâu**

| Nơi | Chứa gì | Thời hạn |
|---|---|---|
| Cơ sở dữ liệu của Bụt (máy chủ tại Việt Nam) | Câu hỏi, câu trả lời, bản tóm tắt cuộc trò chuyện | Tới khi người dùng xoá, rồi thêm 14 ngày |
| Hàng đợi xử lý | Câu hỏi và hồ sơ sức khoẻ đi kèm | 1 ngày |
| Hàng đợi yêu cầu bị lỗi | Như trên, chỉ với yêu cầu xử lý lỗi | 14 ngày |
| Bộ nhớ đệm | Câu trả lời 48 giờ; tóm tắt báo cáo tháng 7 ngày | Tự hết hạn |
| OpenAI (Hoa Kỳ) | Nội dung gửi để sinh câu trả lời | Tới 30 ngày (log giám sát lạm dụng) |

- **Hồ sơ sức khoẻ** (khi bật chia sẻ) chỉ dùng cho đúng lượt hỏi đó, **không** lưu vào lịch sử.
- **Bảo mật:** chỉ người dùng đã đăng nhập mới xem hoặc xoá được lịch sử của chính mình. Cơ sở dữ liệu, hàng đợi và bộ nhớ đệm không mở ra internet.

### Tóm tắt việc cần pháp chế trả lời

| # | Câu hỏi | Gấp |
|---|---|---|
| B2.1 | Hồ sơ chuyển dữ liệu ra nước ngoài và hồ sơ đánh giá tác động: đã nộp chưa, ai lập, ngày chuyển đầu tiên | **Có**, hạn sớm nhất 28/11/2026 |
| B1.1–3 | Xử lý lịch sử đã lưu dưới câu đồng ý cũ | Trước ngày phát hành app |
| B3 | Duyệt câu chữ màn đồng ý | Trước ngày phát hành app |
| B2.2–5 | Bên kiểm soát dữ liệu, DPA và Zero Data Retention với OpenAI, log 30 ngày | |

**Nguồn tham khảo:**
[Luật 91/2025/QH15, Điều 9–23 (LawPlayer)](https://lawplayer.com/vn/act/vn-vbpl-179252/section/chapter-2.section-1) ·
[Luật 91/2025/QH15, tóm tắt (lexdata.vn)](https://lexdata.vn/van-ban-phap-luat/luat-bao-ve-du-lieu-ca-nhan-2025/) ·
[Nghị định 356/2025/NĐ-CP, toàn văn (vbpl.moj.gov.vn)](https://vbpl.moj.gov.vn/TW/Lists/vbpq/Attachments/187276/ND.356.2025.doc) ·
[Tình trạng sức khỏe là dữ liệu nhạy cảm từ 2026 (thuvienphapluat.vn)](https://thuvienphapluat.vn/hoi-dap-phap-luat/tinh-trang-suc-khoe-la-du-lieu-ca-nhan-nhay-cam-tu-nam-2026-dung-khong-138076906.html) ·
[EY: Nghị định 356/2025](https://www.ey.com/vi_vn/technical/tax/tax-and-law-updates/nghi-dinh-so-356-2025-nd-cp-quy-dinh-chi-tiet-mot-so-dieu-va-bien-phap-thi-hanh-luat-bao-ve-du-lieu-ca-nhan) ·
[Frasers: Nghị định hướng dẫn Luật BVDLCN](https://www.frasersvn.com/vi/legal-updates-and-publications/the-next-chapter-in-data-protection-new-decree-guiding-the-personal-data-protection-law) ·
[LSVN: thay đổi từ 01/01/2026](https://lsvn.vn/mot-so-thay-doi-can-luu-y-trong-xu-ly-du-lieu-ca-nhan-tu-01-01-2026-a167892.html) ·
[CDLAF: chuyển dữ liệu xuyên biên giới theo NĐ 356](https://cdlaf.vn/cap-nhat-2026-chuyen-du-lieu-xuyen-bien-gioi-theo-nghi-dinh-356-2025-nd-cp-quy-dinh-va-thu-tuc-tuan-thu/) ·
[Luật Thành Đô: hồ sơ đánh giá tác động chuyển dữ liệu ra nước ngoài](https://luatthanhdo.com.vn/ho-so-danh-gia-tac-dong-xu-ly-du-lieu-ca-nhan-ra-nuoc-ngoai) ·
[OpenAI: Your data](https://developers.openai.com/api/docs/guides/your-data/index.html)
