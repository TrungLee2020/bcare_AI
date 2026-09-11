from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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
