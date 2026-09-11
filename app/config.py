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

    # Bật /test/enqueue (bypass quota, chỉ để verify pipeline Phase 1).
    # Mặc định TẮT — endpoint này bỏ qua quota nên không được bật ở production.
    enable_test_endpoints: bool = False

    def quota_limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.quota_free_per_day,
            "premium": self.quota_premium_per_day,
        }[tier]


settings = Settings()
