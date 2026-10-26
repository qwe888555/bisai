"""并发冒烟：验证同一接口在 N 并发下的表现（T2-09）。

典型用法（报名提交，50 并发，每次带不同 Idempotency-Key）：

    python scripts/smoke_concurrency.py --path /enrollments/enr_1/submit \
        --method POST --json '{}' --concurrency 50 --total 100 \
        --login admin:Foot@2026

只读接口：

    python scripts/smoke_concurrency.py --path /accounts?page=1 --total 100

输出：成功数/失败数、HTTP 状态分布、业务 code 分布、耗时 p50/p95/max。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

DEFAULT_BASE = "http://127.0.0.1:8000/api/v1"


def login(client: httpx.Client, base: str, username: str, password: str) -> str:
    response = client.post(
        f"{base}/auth/login",
        json={"username": username, "password": password},
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("code") != "OK":
        raise SystemExit(f"登录失败：{response.status_code} {payload}")
    return payload["data"]["accessToken"]


def one_call(client: httpx.Client, args, token: str | None, index: int) -> tuple[int, str, float]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if args.idempotency:
        headers["Idempotency-Key"] = f"smoke-{args.run_id}-{index}"
    payload = json.loads(args.json) if args.json else None
    started = time.perf_counter()
    try:
        response = client.request(
            args.method, f"{args.base}{args.path}", json=payload, headers=headers
        )
        elapsed = (time.perf_counter() - started) * 1000
        try:
            code = response.json().get("code", "-")
        except ValueError:
            code = "NON_JSON"
        return response.status_code, code, elapsed
    except Exception as exc:  # 连接层失败也要计入
        elapsed = (time.perf_counter() - started) * 1000
        return 0, type(exc).__name__, elapsed


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--path", required=True, help="如 /accounts?page=1")
    parser.add_argument("--method", default="GET", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    parser.add_argument("--json", default="", help="请求体 JSON 字符串")
    parser.add_argument("--login", default="", help="user:password，留空表示不带令牌")
    parser.add_argument("--token", default="", help="直接给 accessToken")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--idempotency", action="store_true", help="每次请求带不同 Idempotency-Key")
    args = parser.parse_args()

    args.run_id = uuid.uuid4().hex[:8]
    args.base = args.base.rstrip("/")
    if not args.base.endswith("/api/v1"):
        args.base += "/api/v1"
    timeout = httpx.Timeout(30.0)
    with httpx.Client(timeout=timeout) as client:
        token = args.token or None
        if not token and args.login:
            username, _, password = args.login.partition(":")
            token = login(client, args.base, username, password)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(
                pool.map(lambda i: one_call(client, args, token, i), range(args.total))
            )
        wall = time.perf_counter() - started

    status_count: dict[int, int] = {}
    code_count: dict[str, int] = {}
    for status, code, _ in results:
        status_count[status] = status_count.get(status, 0) + 1
        code_count[code] = code_count.get(code, 0) + 1

    latencies = [elapsed for _, _, elapsed in results]
    ok = sum(1 for status, code, _ in results if status == 200 and code == "OK")
    print(f"请求 {args.total} 次 / 并发 {args.concurrency} / 总耗时 {wall:.2f}s")
    print(f"成功 {ok}，失败 {args.total - ok}")
    print("HTTP 状态：", dict(sorted(status_count.items())))
    print("业务 code：", dict(sorted(code_count.items(), key=lambda kv: -kv[1])))
    print(
        "耗时(ms)：p50 %.0f / p95 %.0f / max %.0f / avg %.0f"
        % (
            percentile(latencies, 0.50),
            percentile(latencies, 0.95),
            max(latencies),
            statistics.mean(latencies),
        )
    )
    return 0 if ok == args.total else 1


if __name__ == "__main__":
    sys.exit(main())
