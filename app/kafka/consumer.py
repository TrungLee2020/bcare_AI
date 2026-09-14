import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from app.config import settings
from app.kafka.producer import publish_chat_response, publish_dead_letter
from app.redis_client import get_redis
from app.schemas import ChatRequestMessage, ChatResponseMessage, DeadLetterMessage
from app.services import answering, history, idempotency, quota

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
        await quota.refund(redis, message.user_id)
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
    # vẫn lấy được câu trả lời qua API thay vì mất trắng.
    await idempotency.save_response(redis, message.request_id, response.model_dump(mode="json"))
    await publish_chat_response(response)

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


async def _consume_loop() -> None:
    """
    Phase 1 skeleton: KHÔNG gọi OpenAI ở đây.
    Mục tiêu duy nhất của bước này là verify:
      - Message tới đúng thứ tự trong cùng 1 partition (tức cùng 1 user_id).
      - Consumer group hoạt động đúng khi có nhiều instance (rebalance ổn định).
      - Offset chỉ được commit SAU khi xử lý xong (ở đây là "xử lý" = log),
        để không bao giờ mất message nếu consumer crash giữa chừng.

    enable_auto_commit=False + commit thủ công sau mỗi message là chủ đích,
    không phải quên tắt — tự động commit có thể ack message trước khi biết
    chắc đã xử lý xong, dẫn tới mất message khi consumer crash.
    """
    consumer = AIOKafkaConsumer(
        settings.kafka_topic_chat_requests,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info(
        "Consumer started: topic=%s group=%s",
        settings.kafka_topic_chat_requests,
        settings.kafka_consumer_group,
    )

    try:
        async for record in consumer:
            if _stop_event.is_set():
                break

            try:
                message = ChatRequestMessage.model_validate_json(record.value)

                # Kafka có thể giao lại message này nếu consumer crash trước
                # khi commit offset. Guard ở đây để Phase 3 không gọi OpenAI
                # 2 lần cho cùng 1 câu hỏi.
                if not await idempotency.mark_processing(get_redis(), message.request_id):
                    logger.info(
                        "Bỏ qua request_id=%s (đã được xử lý trước đó)",
                        message.request_id,
                    )
                    await consumer.commit()
                    continue

                logger.info(
                    "Consumed request_id=%s user_id=%s partition=%s offset=%s "
                    "key=%s content_len=%d",
                    message.request_id,
                    message.user_id,
                    record.partition,
                    record.offset,
                    record.key,
                    len(message.content),
                )
                await _handle(message)
            except ValidationError as exc:
                # Message sai schema không bao giờ đúng ở lần thử sau, nên retry
                # là vô nghĩa. Đẩy sang dead-letter để giữ lại mà điều tra rồi
                # đi tiếp, thay vì kẹt mãi ở đúng 1 message hỏng.
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

            # "Ack" = commit offset. Làm sau khi xử lý (kể cả khi lỗi schema ở trên)
            # để tránh consumer bị kẹt lặp lại mãi 1 message hỏng.
            await consumer.commit()
    finally:
        await consumer.stop()
        logger.info("Consumer stopped")


def start_consumer() -> None:
    global _consumer_task
    _stop_event.clear()
    _consumer_task = asyncio.create_task(_consume_loop())


async def stop_consumer() -> None:
    global _consumer_task
    _stop_event.set()
    if _consumer_task is not None:
        await _consumer_task
        _consumer_task = None
