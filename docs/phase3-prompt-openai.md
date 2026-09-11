# Phase 3 — Prompt Engineering & OpenAI Integration

Consumer giờ đã gọi OpenAI thật và publish câu trả lời sang `chat_responses`.

## 4 lớp, không lớp nào tự nó đủ

```
câu hỏi -> [1] input filter (regex) -> [2] system prompt -> OpenAI (structured
outputs) -> [3] output validator -> [4] fallback an toàn -> chat_responses
```

| Lớp | File | Chặn được gì | KHÔNG chặn được gì |
|---|---|---|---|
| 1. Input filter | `app/services/input_filter.py` | mẫu injection phổ biến (VN + EN, có/không dấu) | viết lái, mã hoá, dịch ngôn ngữ khác, nói vòng |
| 2. System prompt | `prompts/system_v1.md` | phần lớn còn lại | model vẫn có thể bị dẫn dụ |
| 3. Output validator | `app/services/output_validator.py` | lộ prompt, kê liều thuốc, chẩn đoán xác định, tự mâu thuẫn | nội dung sai mà vẫn "trông hợp lệ" |
| 4. Fallback | `output_validator.FALLBACK_ANSWER` | đảm bảo user không bao giờ thấy output hỏng | — |

Lớp 1 là regex nên **luôn có thể bị lách** — nó ở đó để chặn sớm cho đỡ tốn
tiền API và để có tín hiệu theo dõi, không phải để làm hàng rào chính.
`tests/injection_corpus.py` cố ý giữ một nhóm `FILTER_MISSES` mà regex không bắt
được, kèm test assert đúng điều đó, để không ai hiểu nhầm lớp này là đủ.

## Prompt có version

`prompts/system_<version>.md`, chọn bằng `PROMPT_VERSION`. Prompt phải chỉnh
liên tục khi thấy model trả lời chưa đạt — để ở file riêng thì sửa prompt không
cần review/deploy lại code, và giữ file version cũ để rollback.

Đổi prompt xong **phải chạy lại `pytest -m live`** (xem dưới): prompt là phần
duy nhất của hệ thống mà unit test không đo được chất lượng.

## Ranh giới chỉ dẫn / dữ liệu

Câu hỏi của user đi trong **user turn riêng**, bọc trong `<user_question>`.
Không bao giờ nối vào system prompt — làm vậy là xoá ranh giới giữa "chỉ dẫn" và
"dữ liệu", mọi câu injection sẽ được model đọc với đúng thẩm quyền của system
prompt. Có test giữ ràng buộc này
(`test_cau_hoi_user_khong_bao_gio_bi_noi_vao_system_prompt`).

## Structured Outputs

Dùng `response_format={"type": "json_schema", ..., "strict": True}` với schema
sinh từ `ChatAnswer`. Thiếu `strict: True` thì json_schema chỉ là gợi ý và model
vẫn có thể trả thiếu field. `ChatAnswer` bắt buộc `extra="forbid"` (strict mode
yêu cầu `additionalProperties: false`) và không field nào có default (mọi field
phải nằm trong `required`) — vì thế `refusal_reason` dùng chuỗi rỗng thay vì
`null`.

## Quota khi bị chặn hay khi lỗi

| Tình huống | Quota | Lý do |
|---|---|---|
| Trả lời bình thường | trừ | — |
| Bị chặn vì injection | **trừ** | hoàn lại thì việc dò injection thành miễn phí, không giới hạn |
| Output không qua validate | trừ | API đã tốn tiền rồi |
| OpenAI lỗi | **hoàn** | user chưa nhận được câu trả lời nào |

Khi OpenAI lỗi, consumer vẫn publish 1 response `status="error"` để SSE không
treo chờ vô hạn. Retry + backoff + dead-letter topic là việc của Phase 5 —
hiện tại lỗi là đi thẳng tới response `error`.

## Test

```bash
pytest -q              # 85 test, dùng client giả, không gọi API
OPENAI_API_KEY=sk-... pytest -m live -q   # 12 test gọi API thật
```

Test thường **không** verify được "model có bị injection chi phối hay không" —
đó là hành vi của model, không phải của code, và chỉ đo được bằng cách gọi thật.
`tests/test_live_openai.py` làm việc đó: bắn nhóm `FILTER_MISSES` (các câu regex
không chặn được) vào model thật và assert model không lộ prompt, không trả lời
nghiêm túc câu injection; có cả case tắt hẳn lớp regex để đo riêng sức đề kháng
của system prompt.

## Còn nợ

- Chưa đo chi phí thực tế/câu hỏi để so với ước tính (Phase 6).
- `openai_model` mặc định `gpt-4o-mini` là lựa chọn rẻ để chạy thử; cần benchmark
  chất lượng trả lời tiếng Việt trước khi chốt cho production.
- Chưa có ngữ cảnh hội thoại — mỗi câu hỏi hiện được trả lời độc lập (Phase 4).
