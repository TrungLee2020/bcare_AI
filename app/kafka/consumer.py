import asyncio
import logging
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer, ConsumerRebalanceListener, TopicPartition
from pydantic import ValidationError

from app import metrics
from app.config import settings
from app.kafka.producer import publish_chat_response, publish_dead_letter
from app.redis_client import get_redis
from app.schemas import ChatRequestMessage, ChatResponseMessage, DeadLetterMessage
from app.services import answering, history, idempotency, quota
from app.sse import replay

logger = logging.getLogger(__name__)

_consumer_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


def _notice(message: ChatRequestMessage, status: str, answer, detail: str = "") -> ChatResponseMessage:
    return ChatResponseMessage(
        request_id=message.request_id,
        user_id=message.user_id,
        session_id=message.session_id,
        status=status,
        answer=answer,
        prompt_version=settings.prompt_version,
        model=settings.openai_model,
        detail=detail,
    )


async def _notify_slow(message: ChatRequestMessage) -> None:
    """
    Quá SLA mà chưa xong thì đẩy 1 event "đang xử lý" để client biết hệ thống
    vẫn đang chạy chứ không phải rớt kết nối.

    Gửi qua đúng topic chat_responses như mọi response khác, vì kết nối SSE của
    user có thể đang nằm ở instance khác — instance này không tự đẩy thẳng vào
    hub của nó được.
    """
    try:
        await asyncio.sleep(settings.sse_processing_notice_seconds)
        await publish_chat_response(
            _notice(message, "processing", answering.PROCESSING_ANSWER, "slow")
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Đây chỉ là tín hiệu giữ nhịp; hỏng thì thôi, không được làm hỏng
        # luồng trả lời chính.
        logger.exception("Không gửi được thông báo đang xử lý cho %s", message.request_id)


async def _deliver(response: ChatResponseMessage) -> None:
    """
    Ghi vào bộ đệm replay rồi publish sang `chat_responses`.

    Bộ đệm replay ghi Ở ĐÂY, đúng một lần cho mỗi response. Trước đây việc này
    nằm ở response_consumer — mà response_consumer chạy trên MỌI instance, nên
    chạy N instance là mỗi response bị ghi N lần: bộ đệm 20 chỗ chỉ còn 20/N
    câu trả lời khác nhau, và client reconnect nhận về hàng loạt bản trùng.

    Ghi trước khi publish: client nhận event xong rớt mạng ngay thì lúc
    reconnect, câu trả lời đó chắc chắn đã có trong bộ đệm.
    """
    await replay.remember(get_redis(), response)
    await publish_chat_response(response)


async def _handle(message: ChatRequestMessage) -> None:
    """Xử lý 1 câu hỏi: sinh câu trả lời, cache lại cho retry, publish sang
    chat_responses."""
    redis = get_redis()
    slow_notice = asyncio.create_task(_notify_slow(message))
    try:
        context = await history.load_context(message)
        response = await answering.answer_question(message, context)
    except Exception as exc:
        # Tới đây là đã retry hết số lần cho phép (xem app/services/retry.py).
        # Hoàn quota (user chưa nhận được câu trả lời nào), đẩy message sang
        # dead-letter để còn điều tra/replay, và vẫn publish 1 response
        # status=error để SSE không treo chờ vô hạn.
        logger.exception("Sinh câu trả lời thất bại request_id=%s", message.request_id)
        await quota.refund(redis, message.user_id, charged_at=message.created_at)
        await _to_dead_letter(
            reason="openai_failed",
            payload=message.model_dump_json(),
            exc=exc,
            attempts=settings.openai_max_attempts,
        )
        response = _notice(message, "error", answering.ERROR_ANSWER, type(exc).__name__)
    finally:
        slow_notice.cancel()

    # Cache trước khi publish: nếu publish lỗi thì client retry cùng request_id
    # vẫn lấy được câu trả lời qua API thay vì mất trắng. Cache này cũng là dấu
    # "đã xử lý xong" mà `_process_record` dựa vào khi Kafka giao lại message.
    await idempotency.save_response(redis, message.request_id, response.model_dump(mode="json"))
    await _deliver(response)

    # Ghi lịch sử SAU khi publish: bước này có thể kéo theo một lần tóm tắt
    # (gọi OpenAI, mất vài giây) — không được để nó làm chậm câu trả lời đang
    # chờ ở phía user.
    #
    # Chỉ ghi lượt status="ok":
    #   - "error" không có câu trả lời thật để lưu.
    #   - "blocked" nếu lưu thì nguyên văn câu injection sẽ được chở lại trong
    #     prompt của mọi câu hỏi sau trong phiên. Muốn phân tích các lần bị
    #     chặn thì đọc log, không phải nhét vào ngữ cảnh.
    if response.status == "ok":
        try:
            await history.record_turn(message, response)
        except Exception:
            # Câu trả lời đã tới tay user rồi; mất một dòng lịch sử không đáng
            # để xử lý lại cả message (và tính tiền OpenAI thêm lần nữa).
            logger.exception(
                "Ghi lịch sử thất bại request_id=%s", message.request_id
            )


async def _to_dead_letter(*, reason: str, payload: str, exc: BaseException | None = None, **extra) -> None:
    """Đẩy sang DLQ. Bản thân bước này hỏng cũng không được làm chết consumer."""
    try:
        await publish_dead_letter(
            DeadLetterMessage(
                reason=reason,
                payload=payload,
                error_type=type(exc).__name__ if exc else "",
                error_detail=str(exc)[:500] if exc else "",
                **extra,
            )
        )
    except Exception:
        logger.exception("Không đẩy được message sang dead-letter (reason=%s)", reason)


async def _process_record(record) -> None:
    """Xử lý 1 record. Mọi lỗi ở đây được ném lên cho vòng lặp quyết định."""
    try:
        message = ChatRequestMessage.model_validate_json(record.value)
    except ValidationError as exc:
        # Message sai schema không bao giờ đúng ở lần thử sau, nên retry là vô
        # nghĩa. Đẩy sang dead-letter để giữ lại mà điều tra rồi đi tiếp, thay
        # vì kẹt mãi ở đúng 1 message hỏng.
        logger.exception(
            "Message tại partition=%s offset=%s không đúng schema",
            record.partition,
            record.offset,
        )
        await _to_dead_letter(
            reason="invalid_schema",
            payload=record.value.decode("utf-8", errors="replace"),
            exc=exc,
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
        )
        return

    # Kafka giao lại message nếu consumer chết trước khi commit offset. Dấu "đã
    # xử lý" là câu trả lời đã cache, KHÔNG phải một cờ "đang xử lý" đặt từ lúc
    # bắt đầu: cờ kiểu đó sống 48h, nên instance bị kill giữa chừng (redeploy,
    # OOM, rebalance) để lại cờ mà không có câu trả lời, và lần giao lại bị bỏ
    # qua — user mất lượt hỏi, SSE chờ mãi không có gì.
    cached = await idempotency.get_cached_response(get_redis(), message.request_id)
    if cached is not None:
        # Đã trả lời xong nhưng có thể chưa kịp publish (chết giữa save_response
        # và publish). Phát lại cho chắc; client dedup theo request_id.
        logger.info(
            "request_id=%s đã có câu trả lời, phát lại thay vì gọi OpenAI lần nữa",
            message.request_id,
        )
        await _deliver(ChatResponseMessage.model_validate(cached))
        return

    age = (datetime.now(timezone.utc) - message.created_at).total_seconds()
    if age > settings.request_max_age_seconds:
        await _expire(message, age)
        return

    logger.info(
        "Consumed request_id=%s user_id=%s partition=%s offset=%s content_len=%d",
        message.request_id,
        message.user_id,
        record.partition,
        record.offset,
        len(message.content),
    )
    await _handle(message)


async def _expire(message: ChatRequestMessage, age: float) -> None:
    """
    Câu hỏi quá cũ: không gọi OpenAI. Người hỏi đã thôi chờ từ lâu, và đây
    thường là dữ liệu test còn sót trong topic (consumer group mới đọc từ đầu)
    hoặc hàng tồn sau khi consumer ngừng lâu. Vẫn hoàn quota nếu còn trong
    ngày, và vẫn đẩy status=error để tab nào còn mở không chờ mãi.
    """
    logger.warning(
        "Bỏ qua request_id=%s vì đã nằm trong topic %.0fs (> %ds)",
        message.request_id,
        age,
        settings.request_max_age_seconds,
    )
    metrics.incr("requests_expired")
    await quota.refund(get_redis(), message.user_id, charged_at=message.created_at)
    response = _notice(message, "error", answering.ERROR_ANSWER, "expired")
    await idempotency.save_response(get_redis(), message.request_id, response.model_dump(mode="json"))
    await _deliver(response)


async def _give_up(record) -> None:
    """Bỏ cuộc một message: cố hoàn quota và báo lỗi xuống SSE để user không
    chờ mãi. Chỉ là "cố": hạ tầng có thể vẫn đang hỏng."""
    try:
        message = ChatRequestMessage.model_validate_json(record.value)
        await quota.refund(get_redis(), message.user_id, charged_at=message.created_at)
        await _deliver(_notice(message, "error", answering.ERROR_ANSWER, "processing_failed"))
    except Exception:
        logger.exception("Không báo lỗi được cho message bị bỏ cuộc")


# Lỗi hạ tầng (Redis/Kafka chập chờn) khi xử lý 1 record: thử lại chính record
# đó, KHÔNG commit và KHÔNG bỏ qua. Bỏ qua là mất câu hỏi của user; để lỗi lọt ra
# ngoài là chết cả vòng lặp consumer. Có trần số lần: lỗi không tự hết (lỗi code)
# mà thử mãi là kẹt vĩnh viễn cả partition — mọi user khác trong đó cùng chờ.
INFRA_RETRY_DELAY_SECONDS = 2.0
INFRA_MAX_ATTEMPTS = 5


async def _process_with_retry(record) -> None:
    for attempt in range(1, INFRA_MAX_ATTEMPTS + 1):
        try:
            await _process_record(record)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt == INFRA_MAX_ATTEMPTS:
                logger.exception(
                    "Bỏ cuộc partition=%s offset=%s sau %d lần, chuyển DLQ",
                    record.partition,
                    record.offset,
                    attempt,
                )
                await _to_dead_letter(
                    reason="processing_failed",
                    payload=record.value.decode("utf-8", errors="replace"),
                    exc=exc,
                    attempts=attempt,
                    topic=record.topic,
                    partition=record.partition,
                    offset=record.offset,
                )
                await _give_up(record)
                return
            logger.exception(
                "Lỗi hạ tầng khi xử lý partition=%s offset=%s (lần %d/%d)",
                record.partition,
                record.offset,
                attempt,
                INFRA_MAX_ATTEMPTS,
            )
            await asyncio.sleep(INFRA_RETRY_DELAY_SECONDS * attempt)


# Chờ message đang xử lý dở xong trước khi tắt / trả partition. Phải lớn hơn
# thời gian xử lý tối đa của 1 message (timeout OpenAI × số lần thử + backoff),
# và nhỏ hơn terminationGracePeriodSeconds của k8s.
SHUTDOWN_GRACE_SECONDS = 90.0
# Phải lớn hơn SHUTDOWN_GRACE_SECONDS, xem chỗ dùng trong `_consume_loop`.
REBALANCE_TIMEOUT_MS = int((SHUTDOWN_GRACE_SECONDS + 30) * 1000)
# Vòng đọc lỗi (Kafka chưa sẵn sàng lúc boot, mất kết nối...) thì thử lại sau
RESTART_DELAY_SECONDS = 2.0


class _PartitionWorker:
    """
    Xử lý TUẦN TỰ các message của đúng 1 partition, commit offset của riêng
    partition đó.

    Tuần tự trong partition là để giữ thứ tự câu hỏi của từng user (partition
    key = user_id). Song song GIỮA các partition là để một instance không chỉ
    chạy 1 lời gọi OpenAI tại một thời điểm: trước đây cả instance xử lý tuần
    tự mọi partition nó giữ, nên một câu chậm (retry OpenAI tới ~80s) làm mọi
    user trên instance đó cùng chờ, và cả hệ thống chỉ được ~60-120 câu/phút.
    """

    def __init__(self, consumer, tp: TopicPartition, limiter: asyncio.Semaphore):
        self.consumer = consumer
        self.tp = tp
        self.limiter = limiter
        self.queue: asyncio.Queue = asyncio.Queue()
        self.busy = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            record = await self.queue.get()
            self.busy = True
            try:
                async with self.limiter:
                    await _process_with_retry(record)
                # "Ack" = commit offset kế tiếp, SAU khi xử lý xong (kể cả khi
                # lỗi schema hay bỏ cuộc): không commit trước để không mất
                # message khi instance chết giữa chừng.
                await self.consumer.commit({self.tp: record.offset + 1})
            except asyncio.CancelledError:
                raise
            except Exception:
                # Commit lỗi (thường do partition vừa bị thu hồi): message sẽ
                # được giao lại, và lần giao lại không gọi OpenAI lần nữa nhờ
                # câu trả lời đã cache (xem `_process_record`).
                logger.exception("Commit offset thất bại %s offset=%s", self.tp, record.offset)
            finally:
                self.busy = False

    async def close(self, grace: float) -> None:
        """Bỏ các message còn xếp hàng (chưa commit -> Kafka giao lại), cho
        message đang dở chạy nốt tối đa `grace` giây rồi huỷ."""
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.busy:
            deadline = asyncio.get_running_loop().time() + grace
            while self.busy and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


class _Workers(ConsumerRebalanceListener):
    def __init__(self, consumer, limiter: asyncio.Semaphore):
        self.consumer = consumer
        self.limiter = limiter
        self.by_tp: dict[TopicPartition, _PartitionWorker] = {}
        self.paused: set[TopicPartition] = set()

    def get(self, tp: TopicPartition) -> _PartitionWorker:
        worker = self.by_tp.get(tp)
        if worker is None:
            worker = self.by_tp[tp] = _PartitionWorker(self.consumer, tp, self.limiter)
        return worker

    def apply_backpressure(self) -> None:
        """Partition nào dồn quá nhiều việc thì tạm ngừng đọc, thay vì kéo cả
        đống message vào RAM; bớt dồn thì đọc tiếp."""
        limit = settings.kafka_max_buffered_per_partition
        for tp, worker in self.by_tp.items():
            size = worker.queue.qsize()
            if size >= limit and tp not in self.paused:
                self.consumer.pause(tp)
                self.paused.add(tp)
            elif size <= limit // 2 and tp in self.paused:
                self.consumer.resume(tp)
                self.paused.discard(tp)

    async def on_partitions_revoked(self, revoked) -> None:
        # Partition sắp chuyển sang instance khác: dừng worker của nó TRƯỚC khi
        # rebalance xong, nếu không 2 instance sẽ cùng xử lý 1 partition.
        await asyncio.gather(
            *(self.by_tp.pop(tp).close(SHUTDOWN_GRACE_SECONDS) for tp in revoked if tp in self.by_tp)
        )
        self.paused.difference_update(revoked)

    async def on_partitions_assigned(self, assigned) -> None:
        pass

    async def close_all(self, grace: float) -> None:
        workers = list(self.by_tp.values())
        self.by_tp.clear()
        await asyncio.gather(*(w.close(grace) for w in workers))


async def _consume_loop() -> None:
    """
    Đọc chat_requests và chia record cho worker của từng partition.

    enable_auto_commit=False + commit thủ công sau mỗi message là chủ đích,
    không phải quên tắt — tự động commit có thể ack message trước khi biết
    chắc đã xử lý xong, dẫn tới mất message khi consumer crash.

    Không vòng lặp nào ở đây được chết vì một lỗi hạ tầng thoáng qua: chết là
    /health vẫn "ok", API vẫn nhận câu hỏi và trừ quota, nhưng không còn ai
    trả lời nữa.
    """
    consumer = AIOKafkaConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        # Mặc định bằng session_timeout_ms (10s), trong khi on_partitions_revoked
        # chờ message đang dở tới SHUTDOWN_GRACE_SECONDS. Để mặc định thì broker
        # loại instance này khỏi group giữa lúc đang chờ, giao partition cho
        # instance khác, và message đang dở bị xử lý lại: 2 lần gọi OpenAI, 2
        # câu trả lời khác nhau cho cùng request_id.
        rebalance_timeout_ms=REBALANCE_TIMEOUT_MS,
    )
    workers = _Workers(consumer, asyncio.Semaphore(settings.max_concurrent_answers))
    consumer.subscribe([settings.kafka_topic_chat_requests], listener=workers)
    try:
        await consumer.start()
    except Exception:
        await consumer.stop()
        raise
    logger.info(
        "Consumer started: topic=%s group=%s concurrency=%d",
        settings.kafka_topic_chat_requests,
        settings.kafka_consumer_group,
        settings.max_concurrent_answers,
    )

    try:
        while not _stop_event.is_set():
            batch = await consumer.getmany(timeout_ms=1000)
            for tp, records in batch.items():
                worker = workers.get(tp)
                for record in records:
                    worker.queue.put_nowait(record)
            workers.apply_backpressure()
    finally:
        await workers.close_all(SHUTDOWN_GRACE_SECONDS if _stop_event.is_set() else 0)
        await consumer.stop()
        logger.info("Consumer stopped")


async def _run_forever() -> None:
    """Chạy lại `_consume_loop` khi nó chết vì lỗi: consumer chết là API vẫn
    nhận câu hỏi và trừ quota nhưng không ai trả lời, và `restart:
    unless-stopped` của Docker không restart container chỉ vì unhealthy.
    Message chưa commit của lần chạy trước được Kafka giao lại."""
    while not _stop_event.is_set():
        try:
            await _consume_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Consumer chat_requests lỗi, khởi động lại sau %.0fs", RESTART_DELAY_SECONDS
            )
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=RESTART_DELAY_SECONDS)
            except asyncio.TimeoutError:
                pass


def start_consumer() -> None:
    global _consumer_task
    _stop_event.clear()
    _consumer_task = asyncio.create_task(_run_forever())


def is_alive() -> bool:
    return _consumer_task is not None and not _consumer_task.done()


async def stop_consumer() -> None:
    """
    Tắt consumer: ngừng đọc (vòng getmany thoát trong ≤1s), cho các message
    đang dở chạy nốt trong khoảng ân hạn, bỏ phần còn xếp hàng (chưa commit nên
    Kafka giao lại cho instance khác), rồi mới đóng kết nối.
    """
    global _consumer_task
    _stop_event.set()
    task, _consumer_task = _consumer_task, None
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=SHUTDOWN_GRACE_SECONDS + 5)
    except asyncio.TimeoutError:
        logger.warning("Consumer không tắt kịp, huỷ")
    except Exception:
        logger.exception("Consumer kết thúc với lỗi")
