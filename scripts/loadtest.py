"""
Load test cho Phase 6 — CẦN Kafka + Redis + Postgres + app đang chạy thật.

Đây là thứ duy nhất kiểm được những thứ unit test không kiểm được:
thứ tự message theo partition với Kafka thật, và hành vi khi nhiều instance
cùng chạy.

    python -m scripts.loadtest --users 50 --questions 3

Kiểm 3 thứ:
1. **Thứ tự theo user**: câu trả lời của cùng 1 user phải về đúng thứ tự đã gửi.
   Sai nghĩa là partition key (user_id) hỏng — xem app/kafka/producer.py.
2. **Quota không race**: tổng số request được chấp nhận phải đúng bằng
   số user x hạn mức, dù gửi đồng thời.
3. **Độ trễ**: p50/p95/p99 để so với SLA đã chốt ở Phase 0.

Lưu ý: script tự phát token bằng AUTH_SECRET trong môi trường, nên chỉ chạy
được ở môi trường test/staging mà bạn giữ secret.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaConsumer

from app.auth import issue_token
from app.config import settings


async def ask(client: httpx.AsyncClient, base_url: str, user_id: int, index: int) -> dict:
    token = issue_token(user_id, "premium")
    started = time.perf_counter()
    try:
        resp = await client.post(
            f"{base_url}/chat/ask",
            json={"request_id": str(uuid4()), "content": f"[{index}] Tôi bị đau đầu"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return {
            "user_id": user_id,
            "index": index,
            "status_code": resp.status_code,
            "request_id": resp.json().get("request_id"),
            "latency": time.perf_counter() - started,
        }
    except Exception as exc:  # noqa: BLE001 - script, không phải thư viện
        return {"user_id": user_id, "index": index, "status_code": 0, "error": str(exc)}


async def collect_responses(expected: int, timeout: float) -> list[dict]:
    """Đọc topic chat_responses để kiểm thứ tự trả về theo từng user."""
    consumer = AIOKafkaConsumer(
        settings.kafka_topic_chat_responses,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=f"loadtest-{uuid4().hex[:8]}",
        auto_offset_reset="latest",
    )
    await consumer.start()
    collected: list[dict] = []
    deadline = time.monotonic() + timeout
    try:
        while len(collected) < expected and time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=1000)
            for records in batch.values():
                for record in records:
                    payload = json.loads(record.value)
                    if payload["status"] != "processing":
                        collected.append(payload)
    finally:
        await consumer.stop()
    return collected


def check_ordering(responses: list[dict]) -> list[str]:
    """Câu trả lời của cùng 1 user phải giữ đúng thứ tự đã gửi."""
    by_user: dict[int, list[int]] = defaultdict(list)
    for payload in responses:
        content = payload.get("answer", {}).get("answer", "")
        marker = content.split("]")[0].lstrip("[") if content.startswith("[") else None
        if marker and marker.isdigit():
            by_user[payload["user_id"]].append(int(marker))

    problems = []
    for user_id, order in by_user.items():
        if order != sorted(order):
            problems.append(f"user {user_id}: nhận theo thứ tự {order}")
    return problems


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--questions", type=int, default=3)
    parser.add_argument("--collect-timeout", type=float, default=120.0)
    args = parser.parse_args()

    user_ids = list(range(900_000, 900_000 + args.users))
    total = args.users * args.questions

    async with httpx.AsyncClient(timeout=30) as client:
        collector = asyncio.create_task(collect_responses(total, args.collect_timeout))
        await asyncio.sleep(1)  # để consumer kịp join group trước khi bắn

        started = time.perf_counter()
        results = await asyncio.gather(
            *(
                ask(client, args.base_url, user_id, index)
                for user_id in user_ids
                for index in range(args.questions)
            )
        )
        elapsed = time.perf_counter() - started
        responses = await collector

        metrics = (await client.get(f"{args.base_url}/metrics")).json()

    accepted = [r for r in results if r["status_code"] == 202]
    rejected = [r for r in results if r["status_code"] == 429]
    failed = [r for r in results if r["status_code"] not in (202, 429, 200)]
    latencies = sorted(r["latency"] for r in accepted if "latency" in r)

    print(f"\n=== Gửi {total} request trong {elapsed:.1f}s ===")
    print(f"  accepted: {len(accepted)}  hết quota: {len(rejected)}  lỗi: {len(failed)}")
    if latencies:
        print(
            f"  latency enqueue p50={statistics.median(latencies)*1000:.0f}ms "
            f"p95={latencies[int(len(latencies)*0.95)]*1000:.0f}ms "
            f"p99={latencies[int(len(latencies)*0.99)]*1000:.0f}ms"
        )

    print(f"\n=== Nhận {len(responses)}/{total} câu trả lời ===")
    problems = check_ordering(responses)
    print("  thứ tự theo user:", "ĐÚNG" if not problems else "SAI")
    for problem in problems[:10]:
        print("   ", problem)

    expected_accepted = args.users * min(
        args.questions, settings.quota_premium_per_day
    )
    print(
        f"\n=== Quota ===\n  kỳ vọng accepted={expected_accepted}, thực tế={len(accepted)}",
        "OK" if len(accepted) == expected_accepted else "LỆCH (nghi race condition)",
    )

    calls = metrics.get("openai_calls", 0)
    cost = metrics.get("cost_usd", 0.0)
    print(f"\n=== Chi phí (ước tính, từ /metrics của 1 instance) ===")
    print(f"  gọi OpenAI: {calls}  tổng: ${cost:.4f}")
    if calls:
        print(f"  trung bình mỗi câu hỏi: ${cost / calls:.5f}")
    print("  (đối chiếu lại đơn giá với bảng giá hiện hành của OpenAI)")


if __name__ == "__main__":
    asyncio.run(main())
