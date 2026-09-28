import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from app.config import settings
from app.kafka.producer import publish_chat_response, publish_dead_letter
from app.redis_client import get_redis
from app.schemas import ChatRequestMessage, ChatResponseMessage, DeadLetterMessage
from app.services import answering, history, idempotency, quota
from app.sse import replay

logger = logging.getLogger(__name__)

_consumer_task: asyncio.Task | None = None
_stop_event = asyncio.Event()
# Đang xử lý dở 1 message (chưa commit). Lúc tắt: đang rảnh thì huỷ ngay, đang
# bận thì cho chạy nốt.
_busy = False


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

    logger.info(
        "Consumed request_id=%s user_id=%s partition=%s offset=%s content_len=%d",
        message.request_id,
        message.user_id,
        record.partition,
        record.offset,
        len(message.content),
    )
    await _handle(message)


async def _give_up(record) -> None:
    """Bỏ cuộc một message: cố hoàn quota và báo lỗi xuống SSE để user không
    chờ mãi. Chỉ là "cố": hạ tầng có thể vẫn đang hỏng."""
    try:
        message = ChatRequestMessage.model_validate_json(record.value)
        await quota.refund(get_redis(), message.user_id)
        await _deliver(_notice(message, "error", answering.ERROR_ANSWER, "processing_failed"))
    except Exception:
        logger.exception("Không báo lỗi được cho message bị bỏ cuộc")


# Lỗi hạ tầng (Redis/Kafka chập chờn) khi xử lý 1 record: thử lại chính record
# đó, KHÔNG commit và KHÔNG bỏ qua. Bỏ qua là mất câu hỏi của user; để lỗi lọt ra
# ngoài là chết cả vòng lặp consumer. Có trần số lần: lỗi không tự hết (lỗi code)
# mà thử mãi là kẹt vĩnh viễn cả partition — mọi user khác trong đó cùng chờ.
INFRA_RETRY_DELAY_SECONDS = 2.0
INFRA_MAX_ATTEMPTS = 5


async def _consume_loop() -> None:
    """
    Đọc chat_requests, xử lý TUẦN TỰ từng message, commit offset SAU khi xử lý.

    enable_auto_commit=False + commit thủ công sau mỗi message là chủ đích,
    không phải quên tắt — tự động commit có thể ack message trước khi biết
    chắc đã xử lý xong, dẫn tới mất message khi consumer crash.

    Vòng lặp này KHÔNG được chết vì một lỗi hạ tầng thoáng qua: trước đây chỉ
    bắt ValidationError, nên Redis rớt 1 giây là task consumer kết thúc trong im
    lặng — /health vẫn "ok", API vẫn nhận câu hỏi và trừ quota, nhưng không còn
    ai trả lời nữa.
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

    global _busy
    try:
        async for record in consumer:
            _busy = True
            for attempt in range(1, INFRA_MAX_ATTEMPTS + 1):
                try:
                    await _process_record(record)
                    break
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
                        break
                    logger.exception(
                        "Lỗi hạ tầng khi xử lý partition=%s offset=%s (lần %d/%d)",
                        record.partition,
                        record.offset,
                        attempt,
                        INFRA_MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(INFRA_RETRY_DELAY_SECONDS * attempt)

            # "Ack" = commit offset. Làm sau khi xử lý (kể cả khi lỗi schema)
            # để không kẹt lặp lại mãi 1 message hỏng.
            await consumer.commit()
            _busy = False
            if _stop_event.is_set():
                break
    finally:
        _busy = False
        await consumer.stop()
        logger.info("Consumer stopped")


def start_consumer() -> None:
    global _consumer_task
    _stop_event.clear()
    _consumer_task = asyncio.create_task(_consume_loop())


def is_alive() -> bool:
    return _consumer_task is not None and not _consumer_task.done()


# Chờ message đang xử lý dở xong trước khi tắt hẳn. Phải lớn hơn thời gian xử
# lý tối đa của 1 message (timeout OpenAI × số lần thử + backoff), và nhỏ hơn
# terminationGracePeriodSeconds của k8s.
SHUTDOWN_GRACE_SECONDS = 90.0


async def stop_consumer() -> None:
    """
    Tắt consumer. Trước đây chỉ đặt cờ rồi `await` task — nhưng vòng lặp chỉ
    xem cờ khi có message MỚI tới, nên topic đang yên là shutdown treo tới khi
    k8s SIGKILL. Giờ: đang rảnh thì huỷ ngay; đang xử lý dở thì cho chạy nốt
    trong một khoảng ân hạn (vòng lặp tự thoát sau khi commit), hết hạn mới
    huỷ. Message bị huỷ giữa chừng chưa được commit nên Kafka sẽ giao lại cho
    instance khác, và instance đó xử lý lại được (xem `_process_record`).
    """
    global _consumer_task
    _stop_event.set()
    task, _consumer_task = _consumer_task, None
    if task is None:
        return
    if _busy and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_GRACE_SECONDS)
            return
        except TimeoutError:
            logger.warning("Message đang xử lý không kịp xong, huỷ để tắt")
        except Exception:
            logger.exception("Consumer kết thúc với lỗi")
            return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
