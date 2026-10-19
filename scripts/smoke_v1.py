"""
Smoke test /v1 trên môi trường thật, bằng tài khoản demo của App.

Chạy TRONG container app (dùng sẵn SUPABASE_URL, SUPABASE_ANON_KEY,
METRICS_TOKEN của server, không phải chép key ra ngoài):

    docker exec -i -e REVIEW_CODE='<mã review>' bcare-ai-app-1 python - < scripts/smoke_v1.py

Mỗi lần chạy tốn khoảng 4 lần gọi OpenAI. Tài khoản demo nằm trong
QUOTA_UNLIMITED_USERS nên không hết lượt. Không in token.
"""

import json
import os
import sys
import time
import uuid

import httpx

from app.config import settings

BASE = os.getenv("BASE_URL", "https://ai.but.care")
EMAIL = os.getenv("REVIEW_EMAIL", "review@modoc.app")
CODE = os.getenv("REVIEW_CODE", "")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'OK' if ok else 'FAIL'}] {name}  {detail}")


def login(client: httpx.Client) -> str:
    resp = client.post(
        f"{settings.supabase_url.rstrip('/')}/functions/v1/demo-login",
        headers={"apikey": settings.supabase_anon_key},
        json={"email": EMAIL, "code": CODE},
    )
    if resp.status_code != 200 or "access_token" not in resp.text:
        sys.exit(f"demo-login lỗi HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["access_token"]


def chat_json(client, token, text, **extra):
    started = time.monotonic()
    resp = client.post(
        f"{BASE}/v1/chat",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", **extra.pop("headers", {})},
        json={"messages": [{"role": "user", "content": text}], "locale": "vi", **extra},
    )
    return resp, time.monotonic() - started


def chat_sse(client, token, text, conversation_id):
    started = time.monotonic()
    first_delta = None
    deltas, done, errors = [], None, []
    with client.stream(
        "POST", f"{BASE}/v1/chat",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        json={"messages": [{"role": "user", "content": text}], "conversation_id": conversation_id, "locale": "vi"},
    ) as resp:
        status = resp.status_code
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "delta":
                first_delta = first_delta or time.monotonic() - started
                deltas.append(event["text"])
            elif event["type"] == "done":
                done = event
            elif event["type"] == "error":
                errors.append(event)
    return status, "".join(deltas), done, errors, first_delta, time.monotonic() - started


def main() -> None:
    if not CODE:
        sys.exit("Thiếu REVIEW_CODE (mã đăng nhập demo, team App gửi riêng).")
    with httpx.Client(timeout=150) as client:
        token = login(client)
        check("demo-login", True, f"email={EMAIL}")

        # 1. JSON, câu mở đầu hội thoại
        resp, took = chat_json(client, token, "Tôi bị đau đầu 3 ngày nay, nên làm gì?")
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        reply = body.get("reply", "")
        check("chat JSON", resp.status_code == 200 and bool(reply),
              f"HTTP {resp.status_code} {took:.1f}s len={len(reply)} quota_remaining={body.get('quota_remaining')} error={body.get('error')}")
        if reply:
            print("    reply:", reply[:200].replace("\n", " "), "…")
        conversation_id = body.get("conversation_id")

        # 2. SSE, nối tiếp cùng hội thoại
        status, text, done, errors, ttfb, took = chat_sse(client, token, "Vẫn đau như vậy thì có cần đi khám không?", conversation_id)
        check("chat SSE nối tiếp", status == 200 and bool(text) and done is not None and not errors,
              f"HTTP {status} {took:.1f}s chữ đầu sau {ttfb or 0:.1f}s len={len(text)} errors={errors}")
        if done:
            check("giữ conversation_id", done.get("conversation_id") == conversation_id,
                  f"should_see_doctor={done.get('should_see_doctor')} follow_ups={len(done.get('follow_up_questions') or [])}")

        # 3. Gửi lại cùng Idempotency-Key: cùng câu trả lời, không tính thêm
        key = {"Idempotency-Key": f"smoke-{uuid.uuid4()}"}
        first, _ = chat_json(client, token, "Uống nhiều nước có giúp đỡ đau đầu không?", headers=key)
        again, took = chat_json(client, token, "Uống nhiều nước có giúp đỡ đau đầu không?", headers=key)
        check("idempotency", first.status_code == again.status_code == 200
              and first.json().get("reply") == again.json().get("reply"), f"lần 2 {took:.1f}s")

        # 4. Cờ cấp cứu: câu cố định, không gọi model
        resp, took = chat_json(client, token, "Tôi đau ngực dữ dội", client_safety="emergency")
        body = resp.json()
        check("client_safety=emergency", resp.status_code == 200 and body.get("should_see_doctor") is True
              and "115" in body.get("reply", ""), f"{took:.2f}s")

        # 5. Báo cáo tháng
        report = {
            "month": "2026-10", "previous_month": "2026-09",
            "blood_pressure": {"avg": "136/85", "prev_avg": "142/89", "high_count": 2, "prev_high_count": 5},
            "medication_adherence": 0.9, "prev_medication_adherence": 0.78,
        }
        started = time.monotonic()
        resp = client.post(f"{BASE}/v1/report/monthly", headers={"Authorization": f"Bearer {token}"}, json=report)
        summary = resp.json().get("summary", "") if resp.status_code == 200 else ""
        check("report/monthly", resp.status_code == 200 and bool(summary),
              f"HTTP {resp.status_code} {time.monotonic() - started:.1f}s len={len(summary)} {'' if summary else resp.text[:150]}")
        if summary:
            print("    summary:", summary[:200].replace("\n", " "), "…")

        # 6. Model nào đã chạy, token suy luận (số liệu của instance này từ lúc khởi động)
        m = client.get("http://127.0.0.1:8000/metrics", headers={"Authorization": f"Bearer {settings.metrics_token}"})
        if m.status_code == 200:
            interesting = {k: v for k, v in m.json().items()
                           if k.split(":")[0] in ("openai_calls", "completion_tokens", "reasoning_tokens", "cost_usd")
                           or k.startswith("v1_")}
            print("    metrics:", json.dumps(interesting, ensure_ascii=False))

    failed = [name for name, ok, _ in results if not ok]
    print("\nKẾT QUẢ:", "TẤT CẢ ĐẠT" if not failed else f"LỖI ở {failed}")
    sys.exit(1 if failed else 0)


main()
