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

    # coerce_numbers_to_str: message cũ (trước khi user_id là UUID Supabase)
    # còn nằm trong topic lúc deploy mang user_id dạng số.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    request_id: UUID = Field(default_factory=uuid4)
    user_id: str
    session_id: UUID | None = None
    tier: Literal["free", "premium"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Câu thứ mấy trong ngày của user, chốt ở API lúc trừ quota (1 = câu đầu,
    # dùng model mạnh — xem Settings.model_for_question). Tính ở API chứ
    # không ở consumer: lúc consumer chạy, counter có thể đã tăng vì các câu
    # gửi sau. None = message cũ, trước khi có trường này.
    question_no: int | None = None
    # Hồ sơ sức khoẻ app gửi kèm (chỉ khi user bật chia sẻ), đã chuẩn hoá ở API
    # (xem app/api/v1.py). Chỉ đưa vào prompt của đúng lượt này, KHÔNG ghi vào
    # lịch sử chat.
    health_context: str | None = None


class V1Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    # Trần rộng cho các lượt cũ (server không dùng chúng, xem app/api/v1.py);
    # lượt user cuối cùng bị kiểm chặt hơn ở endpoint.
    content: str = Field(max_length=8000)


class V1ChatRequest(BaseModel):
    """Body của `POST /v1/chat` — hợp đồng với app (PatronyApp
    docs/ai-chat-api.md). `extra="ignore"`: app thêm trường mới thì service
    cũ vẫn chạy."""

    model_config = ConfigDict(extra="ignore")

    conversation_id: str | None = Field(default=None, max_length=64)
    # "Tối đa 24 lượt" — 1 lượt = 1 hỏi + 1 đáp, nên trần là 48 message.
    messages: list[V1Message] = Field(min_length=1, max_length=48)
    locale: str = Field(default="vi", max_length=16)
    client_safety: Literal["none", "crisis", "emergency"] = "none"
    health_context: dict | None = None


class V1MonthlyReportRequest(BaseModel):
    """Body của `POST /v1/report/monthly`: số liệu TỔNG HỢP 2 tháng do app tự
    tính. Không ép schema chi tiết — app là bên định nghĩa các chỉ số; service
    chỉ chặn kích thước và đưa nguyên khối vào prompt như dữ liệu."""

    model_config = ConfigDict(extra="allow")

    locale: str = Field(default="vi", max_length=16)


class ReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


class ChatAskRequest(BaseModel):
    """
    Body của endpoint chính `POST /chat/ask`.

    KHÔNG có `user_id` và `tier`: hai trường đó đến từ token đã ký (app/auth.py).
    Trước Phase 6 chúng nằm trong body, nghĩa là client chỉ cần gửi
    `tier: "premium"` là được 5 câu/ngày thay vì 2.

    `request_id` nên do CLIENT sinh và giữ nguyên khi retry — đó là thứ duy
    nhất giúp server phân biệt "user hỏi câu mới giống hệt" với "vẫn câu cũ,
    mạng lỗi nên gửi lại". Nếu client không gửi, server tự sinh và mỗi lần
    retry sẽ bị tính là 1 câu hỏi mới (tốn quota).
    """

    request_id: UUID = Field(default_factory=uuid4)
    session_id: UUID | None = None
    content: str = Field(min_length=1, max_length=2000)

    def to_message(
        self, user_id: str, tier: str, question_no: int | None = None
    ) -> "ChatRequestMessage":
        return ChatRequestMessage(
            request_id=self.request_id,
            user_id=user_id,
            session_id=self.session_id,
            tier=tier,
            content=self.content,
            question_no=question_no,
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

    model_config = ConfigDict(coerce_numbers_to_str=True)

    request_id: UUID
    user_id: str
    session_id: UUID | None = None
    # ok = model trả lời bình thường | blocked = bị chặn ở lớp lọc input/output
    # | error = gọi OpenAI lỗi, đã hết retry | processing = chưa xong, chỉ là
    # tín hiệu giữ nhịp cho SSE (không bao giờ là câu trả lời cuối cùng)
    status: Literal["ok", "blocked", "error", "processing"]
    answer: ChatAnswer
    prompt_version: str
    model: str
    # Lý do kỹ thuật khi bị chặn/lỗi — để log và dashboard, KHÔNG hiển thị cho user
    detail: str = ""
    # Token đã dùng + chi phí ước tính của lượt này (rỗng nếu không gọi API)
    usage: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionSummary(BaseModel):
    """Kết quả nén ngữ cảnh của 1 phiên (Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    # Các chủ đề sức khoẻ đã xuất hiện — để sau này dựng báo cáo/dashboard theo
    # thời gian mà không phải đọc lại toàn bộ lịch sử.
    health_topics: list[str]


class DeadLetterMessage(BaseModel):
    """
    Message không xử lý được, đẩy sang `chat_requests_dlq`.

    Giữ nguyên `payload` dạng chuỗi thô: message vào đây thường là vì KHÔNG
    parse được, nên ép nó về schema lần nữa sẽ mất đúng phần cần điều tra.
    """

    # processing_failed = lỗi hạ tầng/lỗi code lặp lại quá số lần thử ở consumer
    reason: Literal["invalid_schema", "openai_failed", "processing_failed"]
    payload: str
    error_type: str = ""
    error_detail: str = ""
    attempts: int = 0
    topic: str = ""
    partition: int | None = None
    offset: int | None = None
    failed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
