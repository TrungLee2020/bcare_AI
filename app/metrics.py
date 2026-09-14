"""
Bộ đếm trong process, phục vụ theo dõi trong lúc rollout.

Cố ý giữ đơn giản: đây là số liệu của MỘT instance, reset khi restart. Đủ để
theo dõi một nhóm nhỏ user trong giai đoạn rollout và để trả lời "chi phí thực
tế so với ước tính". Khi mở rộng thì thay bằng Prometheus exporter, giữ nguyên
tên các chỉ số.
"""

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)


def incr(name: str, value: float = 1) -> None:
    with _lock:
        _counters[name] += value


def snapshot() -> dict[str, float]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()
