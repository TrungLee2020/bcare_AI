from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Cấu hình cho AI-answering service.
    Các biến OpenAI sẽ được thêm ở Phase 3.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_chat_requests: str = "chat_requests"
    kafka_topic_chat_responses: str = "chat_responses"
    kafka_consumer_group: str = "bcare-ai-answering"

    # Số partition mặc định khi tool test tự tạo topic (chỉ dùng cho môi trường dev/local)
    kafka_num_partitions: int = 6
    kafka_replication_factor: int = 1

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Quota theo tier (Phase 0 đã chốt: free 2 câu/ngày, premium 5 câu/ngày)
    quota_free_per_day: int = 2
    quota_premium_per_day: int = 5
    # Quota reset theo ngày ở múi giờ VN, không phải UTC — user hiểu "hôm nay"
    # theo giờ địa phương, nếu dùng UTC thì quota reset lúc 7h sáng.
    quota_timezone: str = "Asia/Ho_Chi_Minh"

    # Idempotency: giữ dấu vết request_id đã xử lý trong 48h (đủ dài cho mọi
    # retry hợp lý từ client, đủ ngắn để không phình Redis).
    dedup_ttl_seconds: int = 172800

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # SLA trả lời đã chốt ở Phase 0; timeout phải nhỏ hơn timeout của SSE để
    # client nhận được thông báo lỗi thay vì treo.
    openai_timeout_seconds: float = 25.0
    openai_max_output_tokens: int = 800
    # Nhiệt độ thấp: đây là nội dung sức khoẻ/bảo hiểm, cần ổn định và bám sát
    # hướng dẫn hơn là sáng tạo.
    openai_temperature: float = 0.2

    # Prompt version đang chạy (file prompts/system_<version>.md).
    # Đổi prompt = đổi biến này, không cần sửa code.
    prompt_version: str = "v1"
    # Chặn câu trả lời dài bất thường (dấu hiệu model lan man hoặc bị dẫn dắt)
    answer_max_chars: int = 2000

    # Bật /test/enqueue (bypass quota, chỉ để verify pipeline Phase 1).
    # Mặc định TẮT — endpoint này bỏ qua quota nên không được bật ở production.
    enable_test_endpoints: bool = False

    def quota_limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.quota_free_per_day,
            "premium": self.quota_premium_per_day,
        }[tier]


settings = Settings()
