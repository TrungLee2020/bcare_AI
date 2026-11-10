# Prompt chat v9 + chèn câu cấp cứu — ghi chép

Ngày: 2026-11-10. Không thuộc ticket riêng; tiếp nối KAN-134 (v8).

## Vì sao

Đo v8 trên 23 câu × 2 lần (cấp cứu, bảo hiểm, triệu chứng thường gặp, thuốc/vitamin, câu gõ chữ tiếng Anh, ngoài phạm vi, câu dễ bịa hoàn cảnh), production dùng `gpt-5-nano` (câu đầu trong ngày `gpt-6-luna`):

- câu từ chối prompt dặn Bụt nói ("mình chỉ hỗ trợ về sức khoẻ và bảo hiểm…") bị chặn là lộ prompt (nano 1/46, luna 4/46) → người dùng nhận "thử hỏi lại";
- nano viết code Python khi được nhờ (không từ chối);
- cấp cứu: câu đầu chưa bảo gọi 115 / cấp cứu ở 3/10 (nano), vd trẻ 2 tháng sốt chỉ "đưa bé đi khám ngay";
- bịa quy trình bồi thường riêng của bCare ("nhận mã hồ sơ" qua ứng dụng);
- câu mở đầu đệm ("Mình chia sẻ vài điều cho bạn", "Mình rất thương bạn"), câu dài tới 1.457 ký tự;
- câu "thông tin tham khảo" gắn cả vào câu trả lời thời tiết;
- nano: chữ lạ ("vatandaş", "xiber", "mất/not"), chữ tiếng Anh 9/46.

## Thay đổi

1. `ALLOWED_PHRASES` (app/prompts.py): thêm câu từ chối "chỉ hỗ trợ về sức khoẻ và bảo hiểm (của bCare)".
2. `prompts/system_v9.md` = v8 +
   - mục "Cách viết": vào thẳng nội dung, không câu đệm; thường dưới 700 ký tự, tối đa 5 gạch đầu dòng; chỉ tiếng Việt chuẩn;
   - ngoài phạm vi gồm cả yêu cầu ngắn (viết code, dịch, bài tập, thời tiết, tin tức, giải trí), kèm câu trả lời mẫu từ chối;
   - bảo hiểm: không bịa quy trình / kênh / mã hồ sơ / thời hạn riêng của bCare; câu trả lời mẫu về hồ sơ bồi thường (giấy tờ thường cần + xem Quy tắc bảo hiểm / gọi tổng đài) nằm trong khối `leak-check: off` — để ở phần quy tắc thì model chép nguyên và bị chặn (đo: 4/5 câu bảo hiểm bị chặn);
   - câu "thông tin tham khảo" chỉ khi trả lời về sức khoẻ, không khi từ chối;
   - thêm từ đổi tiếng Việt: kali, khám từ xa, bệnh có sẵn, khám sức khoẻ định kỳ.
   - `PROMPT_VERSION` mặc định `v9`.
3. Chèn câu cấp cứu bằng code (`output_validator.with_emergency_lead`, gọi trong `answering` sau khi validate OK): câu hỏi khớp `urgent_question` (đau thắt ngực dữ dội / lan tay, tím môi hoặc khó thở dữ dội, co giật, méo miệng / yếu nửa người / nói ngọng đột ngột, mất ý thức, chảy máu không cầm, ý định tự hại, trẻ dưới 3 tháng sốt) mà câu đầu câu trả lời không có "115" / "cấp cứu" / "cơ sở y tế" → chèn "Hãy gọi 115 hoặc đến cơ sở y tế gần nhất ngay." lên đầu, bật `should_see_doctor`, metric `emergency_lead_added`. "Hơi khó thở khi leo cầu thang" không tính.

## Kết quả đo (2026-11-10, cùng 23 câu × 2 lần)

| | v8 nano | v9 nano | v8 luna | v9 luna |
|---|---|---|---|---|
| Không bị chặn | 45/46 | 46/46 | 42/46 | 46/46 |
| Cấp cứu: câu đầu gọi 115 / cấp cứu | 7/10 | 10/10 (4 do code chèn) | 10/10 | 10/10 (0 do code) |
| Từ chối đúng ngoài phạm vi | 3/4 | 4/4 | 4/4 | 4/4 |
| Có chữ tiếng Anh | 9/46 | 9/46 | 0/46 | 0/46 |
| Câu mở đầu đệm | có | 0 | — | 0 |
| Độ dài TB / dài nhất (ký tự) | 697 / 1.457 | 512 / 1.044 | 445 / 755 | 366 / 525 |

Còn lại: nano vẫn chêm từ người dùng gõ (claim, stress, calcium, caffeine, online, "pre-existing condition", "full") — prompt không sửa được; luna không có. Chữ lạ kiểu "vatandaş" không thấy ở lượt đo v9 nhưng là giới hạn của nano. Đổi model chat (OPENAI_MODEL) là quyết định chi phí, chưa làm.
