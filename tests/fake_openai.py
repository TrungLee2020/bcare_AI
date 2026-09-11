import json
from types import SimpleNamespace

from app.schemas import ChatAnswer


def make_answer(**overrides) -> ChatAnswer:
    base = dict(
        answer="Đau đầu kéo dài 3 ngày thường không nguy hiểm, bạn nên nghỉ ngơi "
        "và theo dõi thêm. Nếu đau tăng dần hoặc kèm sốt cao thì nên đi khám.",
        out_of_scope=False,
        refusal_reason="",
        should_see_doctor=False,
        follow_up_questions=["Tôi nên đi khám khoa nào?"],
    )
    base.update(overrides)
    return ChatAnswer(**base)


class FakeOpenAI:
    """Client giả: ghi lại tham số đã gọi và trả về nội dung đặt sẵn."""

    def __init__(self, answer: ChatAnswer | None = None, error: Exception | None = None):
        self.answer = answer if answer is not None else make_answer()
        self.error = error
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        payload = json.dumps(self.answer.model_dump(), ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )
