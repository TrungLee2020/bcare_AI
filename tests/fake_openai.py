import json
from types import SimpleNamespace

from app.schemas import ChatAnswer, SessionSummary


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


def make_summary(**overrides) -> SessionSummary:
    base = dict(
        summary="Người dùng bị đau đầu 3 ngày, đã được khuyên nghỉ ngơi và theo dõi.",
        health_topics=["đau đầu"],
    )
    base.update(overrides)
    return SessionSummary(**base)


class FakeOpenAI:
    """Client giả: ghi lại tham số đã gọi và trả về nội dung đặt sẵn."""

    def __init__(
        self,
        answer: ChatAnswer | None = None,
        error: Exception | None = None,
        summary: SessionSummary | None = None,
    ):
        self.answer = answer if answer is not None else make_answer()
        self.summary = summary if summary is not None else make_summary()
        self.error = error
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        # Cùng 1 client giả phục vụ cả 2 loại call; phân biệt bằng tên schema.
        name = kwargs.get("response_format", {}).get("json_schema", {}).get("name")
        result = self.summary if name == "session_summary" else self.answer
        payload = json.dumps(result.model_dump(), ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))],
            usage=SimpleNamespace(prompt_tokens=800, completion_tokens=200),
        )
