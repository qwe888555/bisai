"""成员五 events 冒烟：正常建赛事 / 403 越权 / 409 版本冲突。

用法（backend 目录）：
    .venv/Scripts/python.exe scripts/smoke_events.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if not cond:
        failures.append(name)


def login(client: Client, username: str) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        data=json.dumps({"username": username, "password": "Foot@2026"}),
        content_type="application/json",
    )
    body = resp.json()
    return body["data"]["accessToken"]


def auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def main() -> int:
    admin = Client()
    admin_token = login(admin, "admin")

    # 1) 正常创建赛事
    payload = {
        "name": "2026 省青少年足球联赛",
        "season": "2026",
        "eventCategory": "LEAGUE",
        "registrationOpensAt": "2026-09-01T00:00:00+08:00",
        "registrationClosesAt": "2026-09-20T23:59:59+08:00",
        "status": "DRAFT",
        "description": "冒烟测试赛事",
    }
    resp = admin.post(
        "/api/v1/events",
        data=json.dumps(payload),
        content_type="application/json",
        **auth(admin_token),
    )
    body = resp.json()
    check("创建赛事 200/OK", resp.status_code == 200 and body["code"] == "OK",
          f"status={resp.status_code} code={body.get('code')}")
    event_id = body["data"]["id"]
    check("返回驼峰字段", body["data"].get("registrationOpensAt") is not None
          and "eventCategory" in body["data"])

    # 2) 详情
    resp = admin.get(f"/api/v1/events/{event_id}", **auth(admin_token))
    check("赛事详情 200", resp.status_code == 200 and resp.json()["code"] == "OK")

    # 3) 403：team 角色无权 eventList
    team = Client()
    team_token = login(team, "team")
    resp = team.get("/api/v1/events", **auth(team_token))
    check("越权访问 403/FORBIDDEN",
          resp.status_code == 403 and resp.json()["code"] == "FORBIDDEN",
          f"status={resp.status_code} code={resp.json().get('code')}")

    # 4) 409：PUT 带错误 version
    update = dict(payload)
    update["version"] = 999
    resp = admin.put(
        f"/api/v1/events/{event_id}",
        data=json.dumps(update),
        content_type="application/json",
        **auth(admin_token),
    )
    check("版本冲突 409/VERSION_CONFLICT",
          resp.status_code == 409 and resp.json()["code"] == "VERSION_CONFLICT",
          f"status={resp.status_code} code={resp.json().get('code')}")

    # 5) 正常更新（带正确 version=0）
    update["version"] = 0
    resp = admin.put(
        f"/api/v1/events/{event_id}",
        data=json.dumps(update),
        content_type="application/json",
        **auth(admin_token),
    )
    check("正确版本更新 200", resp.status_code == 200 and resp.json()["code"] == "OK",
          f"status={resp.status_code}")

    # 6) 未知字段 400（StrictSchema）
    bad = dict(payload)
    bad["unexpectedField"] = "x"
    resp = admin.post(
        "/api/v1/events",
        data=json.dumps(bad),
        content_type="application/json",
        **auth(admin_token),
    )
    check("未知字段 400/INVALID_ARGUMENT",
          resp.status_code == 400 and resp.json()["code"] == "INVALID_ARGUMENT",
          f"status={resp.status_code} code={resp.json().get('code')}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())