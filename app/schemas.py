from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ChatRequestMessage(BaseModel):
    """
    Message contract cho topic `chat_requests`.

    Partition key = user_id (xem app/kafka/producer.py) để đảm bảo mọi request
    của cùng 1 user luôn được xử lý theo đúng thứ tự gửi lên, kể cả khi có
    nhiều consumer instance chạy song song trong cùng consumer group.
    """

    request_id: UUID = Field(default_factory=uuid4)
    user_id: int
    session_id: UUID | None = None
    tier: Literal["free", "premium"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatAskRequest(BaseModel):
    """
    Body của endpoint chính `POST /chat/ask`.

    `request_id` nên do CLIENT sinh và giữ nguyên khi retry — đó là thứ duy
    nhất giúp server phân biệt "user hỏi câu mới giống hệt" với "vẫn câu cũ,
    mạng lỗi nên gửi lại". Nếu client không gửi, server tự sinh và mỗi lần
    retry sẽ bị tính là 1 câu hỏi mới (tốn quota).
    """

    request_id: UUID = Field(default_factory=uuid4)
    user_id: int
    session_id: UUID | None = None
    tier: Literal["free", "premium"]
    content: str = Field(min_length=1, max_length=2000)

    def to_message(self) -> "ChatRequestMessage":
        return ChatRequestMessage(
            request_id=self.request_id,
            user_id=self.user_id,
            session_id=self.session_id,
            tier=self.tier,
            content=self.content,
        )


class ChatAskResponse(BaseModel):
    request_id: UUID
    # accepted = vừa nhận, đang xử lý | duplicate = request_id đã thấy rồi,
    # đang xử lý, không tốn thêm quota | done = đã có câu trả lời (Phase 3)
    status: Literal["accepted", "duplicate", "done"]
    quota_remaining: int | None = None
    answer: dict | None = None


REFUSAL_REASONS = ("diagnosis", "prescription", "legal", "off_topic", "injection")


class ChatAnswer(BaseModel):
    """
    Format câu trả lời, được ép cứng bằng Structured Outputs (json_schema +
    strict) chứ không dựa vào việc "nhờ" model trả JSON trong prompt — cách sau
    thỉnh thoảng vẫn trả text thường và làm FE vỡ.

    `extra="forbid"` là bắt buộc: strict mode của OpenAI yêu cầu
    additionalProperties=false, và không field nào được có default (mọi field
    phải nằm trong `required`).
    """

    model_config = ConfigDict(extra="forbid")

    answer: str
    out_of_scope: bool
    # "" khi out_of_scope=false. Dùng chuỗi rỗng thay vì null vì strict mode
    # xử lý enum đơn giản hơn nhiều so với anyOf[string, null].
    refusal_reason: Literal["", "diagnosis", "prescription", "legal", "off_topic", "injection"]
    should_see_doctor: bool
    follow_up_questions: list[str]


class ChatResponseMessage(BaseModel):
    """Message contract cho topic `chat_responses` (SSE layer sẽ đọc từ đây)."""

    request_id: UUID
    user_id: int
    session_id: UUID | None = None
    # ok = model trả lời bình thường | blocked = bị chặn ở lớp lọc input/output
    # | error = gọi OpenAI lỗi, đã hết retry
    status: Literal["ok", "blocked", "error"]
    answer: ChatAnswer
    prompt_version: str
    model: str
    # Lý do kỹ thuật khi bị chặn/lỗi — để log và dashboard, KHÔNG hiển thị cho user
    detail: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionSummary(BaseModel):
    """Kết quả nén ngữ cảnh của 1 phiên (Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    # Các chủ đề sức khoẻ đã xuất hiện — để sau này dựng báo cáo/dashboard theo
    # thời gian mà không phải đọc lại toàn bộ lịch sử.
    health_topics: list[str]
