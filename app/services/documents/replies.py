"""Câu trả lời cố định khi user gửi ảnh không kèm câu hỏi. API trả câu này
ngay, consumer ghi đúng câu này vào lịch sử: một chỗ để hai bên khớp nhau."""

from app.schemas import ChatAnswer


def received_answer(count: int) -> ChatAnswer:
    return ChatAnswer(
        answer=(
            f"Bụt đã nhận {count} ảnh, đang đọc và lưu vào hồ sơ sức khoẻ của bạn. "
            "Bạn cần hỏi gì về giấy tờ này thì cứ nhắn mình nhé."
        ),
        out_of_scope=False,
        refusal_reason="",
        should_see_doctor=False,
        follow_up_questions=[],
    )
