from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Cấu hình cho AI-answering service.
    Các biến OpenAI sẽ được thêm ở Phase 3.
    """

    # extra="ignore": `.env` dùng chung với docker-compose (POSTGRES_PASSWORD,
    # APP_PORT...). Mặc định của pydantic-settings là cấm biến lạ trong file
    # .env, nên chỉ cần thêm một biến cho compose là app, pytest và mọi script
    # chạy ngoài Docker đều không khởi động được.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # "production" bật các kiểm tra fail-closed lúc khởi động (app/main.py):
    # thiếu secret, còn bật endpoint test, topic Kafka ít bản sao... thì từ
    # chối chạy thay vì chạy "tạm" với cấu hình của môi trường test.
    app_env: Literal["dev", "production"] = "dev"

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_chat_requests: str = "chat_requests"
    kafka_topic_chat_responses: str = "chat_responses"
    kafka_consumer_group: str = "bcare-ai-answering"

    # Số partition khi tạo topic (scripts/create_topics.py). Là TRẦN số message
    # xử lý song song của cả hệ thống: mỗi partition một worker tuần tự (để giữ
    # thứ tự theo user). Tăng số partition của topic đang chạy sẽ đổi user nào
    # rơi vào partition nào — chỉ làm khi topic đang trống.
    kafka_num_partitions: int = 24
    # Production: 3 (kèm min.insync.replicas=2). 1 chỉ dành cho local.
    kafka_replication_factor: int = 1
    kafka_min_insync_replicas: int = 1
    # Số bản sao tối thiểu mà APP_ENV=production đòi ở topic có sẵn. Hạ xuống 1
    # chỉ khi CỐ Ý chạy production trên cụm 1 broker (vd docker-compose.yml):
    # chấp nhận mất câu hỏi khi broker chết, nhưng KHÔNG phải tắt luôn các
    # kiểm tra bảo mật như khi để APP_ENV=dev.
    kafka_production_min_replication: int = 3
    # Số câu trả lời một instance xử lý CÙNG LÚC (qua mọi partition nó giữ).
    # Chặn trên bởi rate limit OpenAI của tài khoản (RPM/TPM) chia cho số
    # instance, và bởi DB_POOL_SIZE + DB_MAX_OVERFLOW.
    max_concurrent_answers: int = 16
    # Partition dồn quá ngần này message chưa xử lý thì tạm ngừng đọc nó.
    kafka_max_buffered_per_partition: int = 50
    # Câu hỏi nằm trong topic lâu hơn ngần này thì bỏ qua, không gọi OpenAI:
    # người hỏi đã thôi chờ từ lâu. Chặn luôn trường hợp consumer group mới
    # (auto_offset_reset=earliest) đọc lại dữ liệu test cũ còn trong topic, và
    # trường hợp consumer ngừng lâu rồi chạy lại trả lời cả đống câu hỏi cũ.
    request_max_age_seconds: int = 600

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
    # Model cho câu hỏi THỨ 1 TRONG NGÀY của mỗi user (đếm theo quota, reset
    # 0h giờ VN): model mạnh cho câu đầu, các câu sau và bước tóm tắt dùng
    # OPENAI_MODEL (model nhỏ, rẻ). Trống = mọi câu dùng OPENAI_MODEL.
    # Lượt bị hoàn quota (lỗi hệ thống) thì lần hỏi lại vẫn là câu thứ 1.
    openai_model_first: str = ""
    # SLA trả lời đã chốt ở Phase 0; timeout phải nhỏ hơn timeout của SSE để
    # client nhận được thông báo lỗi thay vì treo.
    openai_timeout_seconds: float = 25.0
    # Phải đủ chỗ cho cả `answer` (~1500 ký tự tiếng Việt ≈ 600 token) lẫn
    # `follow_up_questions` và phần khung JSON. Cắt cụt giữa chừng thì JSON
    # hỏng -> parse lỗi -> tốn tiền API mà user không nhận được gì, nên để dư
    # hơn là để vừa khít: chỉ token SINH RA mới bị tính tiền, trần cao không
    # làm đắt thêm câu trả lời ngắn.
    openai_max_output_tokens: int = 1000
    # Nhiệt độ thấp: đây là nội dung sức khoẻ/bảo hiểm, cần ổn định và bám sát
    # hướng dẫn hơn là sáng tạo.
    openai_temperature: float = 0.2
    # Chỉ áp dụng cho model reasoning (gpt-5*, o*), thay cho temperature.
    # Thấp = nhanh, rẻ, và ít nguy cơ tiêu hết max_completion_tokens vào suy luận.
    openai_reasoning_effort: str = "low"
    # Token suy luận được tính vào max_completion_tokens: cộng thêm khoản này
    # cho model reasoning, không thì hết ngân sách giữa chừng và trả về rỗng.
    openai_reasoning_token_budget: int = 2000

    # Prompt version đang chạy (file prompts/system_<version>.md).
    # Đổi prompt = đổi biến này, không cần sửa code.
    prompt_version: str = "v4"
    # Chặn câu trả lời dài bất thường (dấu hiệu model lan man hoặc bị dẫn dắt)
    answer_max_chars: int = 2000

    # Postgres (lịch sử chat)
    database_url: str = "postgresql+asyncpg://bcare:bcare@localhost:5432/bcare"
    # Phải đủ cho MAX_CONCURRENT_ANSWERS câu xử lý cùng lúc.
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # Ngữ cảnh hội thoại: số message gần nhất chở nguyên văn vào prompt
    # (10 message = 5 lượt hỏi-đáp). Tăng số này là tăng thẳng chi phí mỗi
    # câu hỏi, nên đổi thì phải đo lại chi phí.
    history_window_messages: int = 10
    # Trần độ dài MỖI message cũ khi chở lại vào prompt. Không có trần thì
    # 10 message × 2000 ký tự (giới hạn của ChatAskRequest) được chở lại ở MỌI
    # câu hỏi sau trong phiên — user tự bơm chi phí lên vài lần chỉ bằng cách
    # gửi câu hỏi thật dài. Phần bị cắt vẫn còn nguyên trong DB và trong summary.
    history_message_max_chars: int = 600
    # Trần độ dài transcript gửi đi tóm tắt, vì lý do y hệt.
    summary_transcript_max_chars: int = 8000
    # Tóm tắt khi số message chưa tóm tắt vượt ngưỡng này
    summary_trigger_messages: int = 20
    summary_prompt_version: str = "v3"
    summary_max_chars: int = 1500
    # Tóm tắt thất bại (lỗi API, bị cắt cụt, summary không qua kiểm tra an
    # toàn) thì chờ thêm ngần này message mới thử lại. Không có khoảng chờ thì
    # một phiên có summary luôn bị từ chối (vd người dùng từng thử injection và
    # model cứ chép lại) sẽ tốn thêm một lần gọi tóm tắt ở MỌI lượt hỏi sau.
    summary_retry_after_messages: int = 6

    # Retry khi gọi OpenAI lỗi tạm thời (rate limit, timeout, 5xx)
    openai_max_attempts: int = 3
    openai_retry_base_delay: float = 1.0
    openai_retry_max_delay: float = 8.0
    # Message hỏng hoặc lỗi hết retry được đẩy sang đây thay vì bị bỏ im lặng
    kafka_topic_dead_letter: str = "chat_requests_dlq"

    # SSE
    # Nhịp heartbeat: proxy/load balancer thường đóng kết nối idle sau 30-60s,
    # gửi comment rỗng định kỳ để giữ kết nối sống.
    sse_heartbeat_seconds: float = 15.0
    # Quá mốc này mà chưa trả lời xong thì đẩy 1 event "đang xử lý" để client
    # không tưởng mất kết nối. Phải NHỎ HƠN openai_timeout_seconds.
    sse_processing_notice_seconds: float = 5.0
    # Số response gần nhất giữ lại cho mỗi user để replay khi client reconnect
    sse_replay_buffer_size: int = 20
    sse_replay_ttl_seconds: int = 3600
    # Kết nối MỚI (không kèm mốc) vẫn được phát lại câu trả lời phát ra trong
    # ngần này giây gần nhất. Không có nó thì câu trả lời tới NHANH HƠN lúc SSE
    # kịp mở (vd câu bị chặn ở lớp input: vài ms) sẽ không bao giờ tới client.
    # Để ngắn: mở lại trang trong khoảng này sẽ nhận lại câu trả lời vừa rồi
    # (cùng id, client bỏ qua được).
    sse_replay_on_connect_seconds: int = 10
    # Trần số kết nối SSE đang mở của MỘT user trên một instance (nhiều tab).
    # Không có trần thì một token mở được vô hạn kết nối, mỗi cái một hàng đợi.
    sse_max_connections_per_user: int = 5
    # Kết nối sống quá ngần này thì server chủ động đóng; EventSource tự nối
    # lại (kèm Last-Event-ID) và token được kiểm lại từ đầu — không thì một
    # kết nối mở bằng token sắp hết hạn vẫn nghe tiếp mãi mãi.
    sse_max_connection_seconds: int = 1800

    # Xác thực (Phase 6). Mặc định BẬT: user_id/tier lấy từ token đã ký, không
    # phải từ body do client gửi.
    auth_required: bool = True
    auth_secret: str = ""

    # Rollout dần
    rollout_enabled: bool = True
    # % user được bật, chia theo hash của user_id. 0 = chỉ allowlist.
    rollout_percentage: int = 0
    # Danh sách user_id luôn được bật, cách nhau bằng dấu phẩy (nhóm nội bộ)
    rollout_allowlist: str = ""

    # Giá OpenAI (USD / 1 triệu token) để ước tính chi phí.
    # !! Phải đối chiếu lại với bảng giá hiện hành của OpenAI trước khi tin số
    # liệu báo cáo — giá thay đổi theo thời gian và theo model.
    price_input_per_1m: float = 0.15
    price_output_per_1m: float = 0.60
    # Đơn giá riêng theo model khi dùng nhiều model (OPENAI_MODEL_FIRST):
    # "model=in,out;model2=in,out" (USD / 1M token). Model không có ở đây thì
    # dùng PRICE_INPUT_PER_1M / PRICE_OUTPUT_PER_1M ở trên.
    model_prices: str = ""

    # Bật /test/enqueue (bypass quota, chỉ để verify pipeline Phase 1).
    # Mặc định TẮT — endpoint này bỏ qua quota nên không được bật ở production.
    enable_test_endpoints: bool = False

    # Bearer token để đọc /metrics (số liệu chi phí, lưu lượng). Trống = không
    # cần token (chỉ dành cho dev); production bắt buộc đặt.
    metrics_token: str = ""

    def model_for_question(self, question_no: int | None) -> str:
        """Model cho câu hỏi thứ `question_no` trong ngày của user. None =
        message cũ trước khi có trường này -> model mặc định."""
        if question_no == 1 and self.openai_model_first:
            return self.openai_model_first
        return self.openai_model

    def quota_limit_for_tier(self, tier: str) -> int:
        return {
            "free": self.quota_free_per_day,
            "premium": self.quota_premium_per_day,
        }[tier]


settings = Settings()
