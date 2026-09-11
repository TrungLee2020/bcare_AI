from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Cấu hình cho AI-answering service.
    Phase 1 chỉ cần các biến liên quan tới Kafka; các biến khác (Redis, OpenAI,
    quota...) sẽ được thêm dần ở Phase 2/3.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_chat_requests: str = "chat_requests"
    kafka_topic_chat_responses: str = "chat_responses"
    kafka_consumer_group: str = "bcare-ai-answering"

    # Số partition mặc định khi tool test tự tạo topic (chỉ dùng cho môi trường dev/local)
    kafka_num_partitions: int = 6


settings = Settings()
