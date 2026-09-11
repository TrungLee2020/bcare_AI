# Phase 4 — Context & History

Từ phase này consumer nhớ được mạch hội thoại: "vẫn đau như hôm qua" mới có
nghĩa với model.

## Ngữ cảnh gồm 2 phần

```
prompt = system prompt
       + <session_summary>   (đã nén, phần lịch sử CŨ)
       + N message gần nhất  (nguyên văn, phần lịch sử MỚI)
       + <user_question>     (câu hỏi hiện tại)
```

`chat_sessions.summarized_through_id` là mốc chia đôi hai phần đó: message có
`id <= mốc` thì đã nằm trong summary, message sau mốc mới chở nguyên văn. Thiếu
mốc này thì không biết tin nào đã được tóm tắt — vừa nhồi trùng vào prompt, vừa
tóm tắt lại từ đầu mỗi lần.

Nhờ vậy **chi phí mỗi câu hỏi gần như không tăng theo độ dài phiên**: phiên dài
ra thì summary dày lên chút ít, còn phần nguyên văn luôn bị chặn ở
`HISTORY_WINDOW_MESSAGES`.

## Summary là bề mặt tấn công, không chỉ là tối ưu chi phí

Summary do **model sinh ra** rồi được **nạp lại** vào prompt của mọi câu hỏi sau
trong phiên. Đó là một đường "rửa" injection: câu độc nằm trong lịch sử → lọt
vào bản tóm tắt → nếu tóm tắt được đặt ở system turn thì nó vừa leo từ dữ liệu
lên thành chỉ dẫn hệ thống.

Ba biện pháp, có test giữ cả ba:
1. Summary luôn đi ở **user turn**, trong khối `<session_summary>` — không bao
   giờ nối vào system prompt.
2. Lịch sử cũ của user cũng được bọc `<user_question>` y như câu hỏi mới.
3. Summary bị **soi trước khi ghi xuống DB** (lộ system prompt, quá dài → không
   ghi). Một summary nhiễm sẽ đầu độc toàn bộ phần còn lại của phiên, nên chặn
   ở bước ghi rẻ hơn nhiều so với chặn ở từng lượt đọc.

Prompt hệ thống lên `v2` để nói rõ với model rằng phần ngữ cảnh cũng chỉ là dữ
liệu. `v1` vẫn giữ nguyên trong `prompts/` để rollback được — đây chính là lý do
prompt được đánh version từ Phase 3.

## Lượt nào được ghi vào lịch sử

| status | Ghi? | Lý do |
|---|---|---|
| `ok` | có | — |
| `blocked` | **không** | ghi thì nguyên văn câu injection sẽ được chở lại trong prompt của mọi câu hỏi sau trong phiên. Muốn phân tích thì đọc log. |
| `error` | **không** | không có câu trả lời thật để lưu |

Không có `session_id` thì không có ngữ cảnh — mỗi câu hỏi đứng một mình. **FE
cần gửi kèm `session_id`** nếu muốn hội thoại nhớ được mạch trước đó.

## Tóm tắt chạy ở đâu

Chạy thẳng trong consumer sau mỗi `SUMMARY_TRIGGER_MESSAGES` message, không phải
cron job riêng. Lý do: consumer đã xử lý tuần tự theo từng partition (1 user = 1
partition) nên không bao giờ có 2 tiến trình cùng tóm tắt 1 phiên — khỏi cần
khoá phân tán.

**Đánh đổi:** cứ K message lại có đúng 1 message phải chờ thêm một lần gọi OpenAI
nữa. Để giảm, đã đặt bước ghi lịch sử **sau** khi publish câu trả lời, nên độ trễ
này không rơi vào người dùng đang chờ — nhưng nó vẫn chiếm chỗ của message kế
tiếp trong cùng partition. Nếu sau này thấy ảnh hưởng SLA thì chuyển tóm tắt
sang worker riêng (đọc từ một topic `summary_jobs`), lúc đó mới cần khoá.

## Schema

`app/db/models.py` là **nguồn sự thật duy nhất**. `db/schema.sql` được sinh ra:

```bash
python -m scripts.dump_schema
```

Chạy lại mỗi khi sửa model và commit kèm. Chưa dùng migration tool — khi schema
bắt đầu phải đổi trên dữ liệu thật thì chuyển sang Alembic (Phase 6).

`uq_chat_messages_request_role` là unique index trên `(request_id, role)`, không
phải `request_id` đơn lẻ: mỗi lượt hỏi sinh ra 2 dòng (user + assistant) dùng
chung `request_id`. Ràng buộc này là lớp idempotency **không phụ thuộc Redis**:
Redis mất dữ liệu thì lịch sử vẫn không bị nhân đôi.

## Test

```bash
pytest -q    # 107 test
```

Test DB chạy trên **SQLite in-memory qua aiosqlite** — SQL thật (transaction,
khoá ngoại, unique index đều có hiệu lực) mà CI không cần dựng Postgres. Khác
biệt cần nhớ: `JSONB` -> `JSON`, `BIGSERIAL` -> `INTEGER`. Các truy vấn ở
`repository.py` đều là SQL chuẩn, nhưng **schema thật vẫn chưa được kiểm trên
Postgres** — cần làm trước khi rollout.

## Còn nợ

- Chưa chạy thử trên Postgres thật (chỉ có SQLite trong test).
- Bảng `user_health_insights` (phân tích xu hướng sức khoẻ dài hạn) vẫn là
  optional trong plan, chưa làm. `SessionSummary.health_topics` đã được sinh ra
  và có thể dùng làm đầu vào cho nó sau này, nhưng hiện chưa được lưu riêng.
- Chưa có cơ chế xoá/ẩn lịch sử theo yêu cầu người dùng — cần cho dữ liệu sức
  khoẻ trước khi lên production.
