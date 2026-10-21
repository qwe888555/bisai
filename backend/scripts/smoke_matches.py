"""T5-02 比赛对阵（5.1）冒烟：创建/详情/列表过滤/403/409/发布/下线/400。

用法（backend 目录）：
    .venv/Scripts/python.exe scripts/smoke_matches.py
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
    return resp.json()["data"]["accessToken"]


def auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def main() -> int:
    admin = Client()
    admin_token = login(admin, "admin")

    # 前置：建赛事与组别（match 依赖）
    ev = admin.post(
        "/api/v1/events",
        data=json.dumps({
            "name": "T5-02 冒烟联赛",
            "season": "2026",
            "eventCategory": "LEAGUE",
            "registrationOpensAt": "2026-09-01T00:00:00+08:00",
            "registrationClosesAt": "2026-09-20T23:59:59+08:00",
            "status": "DRAFT",
            "description": "match 冒烟",
        }),
        content_type="application/json", **auth(admin_token),
    )
    event_id = ev.json()["data"]["id"]
    gr = admin.post(
        f"/api/v1/events/{event_id}/groups",
        data=json.dumps({
            "name": "小学男子甲组", "category": "PRIMARY", "gender": "MALE",
            "quota": 0, "maxPlayers": 11, "requiredMaterialTypes": ["PLAYER_CARD"],
        }),
        content_type="application/json", **auth(admin_token),
    )
    group_id = gr.json()["data"]["id"]

    # 1) 创建对阵
    payload = {
        "eventId": event_id,
        "groupId": group_id,
        "homeTeamId": "team_a",
        "awayTeamId": "team_b",
        "kickoffAt": "2026-10-25T15:00:00+08:00",
        "venue": "奥体中心1号场",
        "stage": "小组赛第1轮",
        "status": "SCHEDULED",
        "referees": [{"name": "张裁判", "role": "REFEREE", "level": "国家级"}],
    }
    resp = admin.post("/api/v1/matches", data=json.dumps(payload),
                      content_type="application/json", **auth(admin_token))
    body = resp.json()
    check("创建对阵 200/OK", resp.status_code == 200 and body["code"] == "OK",
          f"status={resp.status_code} code={body.get('code')}")
    m = body["data"]
    match_id = m["id"]
    check("初始 published=false", m["published"] is False)
    check("referees 原样保留", len(m["referees"]) == 1 and m["referees"][0]["role"] == "REFEREE")
    check("version=0", m["version"] == 0)

    # 2) 详情
    resp = admin.get(f"/api/v1/matches/{match_id}", **auth(admin_token))
    check("对阵详情 200", resp.status_code == 200 and resp.json()["code"] == "OK")

    # 3) 列表按 status / teamId 过滤
    resp = admin.get(f"/api/v1/matches?status=SCHEDULED&teamId=team_a", **auth(admin_token))
    page = resp.json()["data"]
    check("列表按状态+球队过滤命中",
          resp.status_code == 200 and any(i["id"] == match_id for i in page["items"]),
          f"total={page['total']}")
    resp = admin.get("/api/v1/matches?teamId=team_zzz", **auth(admin_token))
    check("无关球队过滤为空", resp.json()["data"]["total"] == 0)

    # 4) 403：team 无权 matchList
    team = Client()
    team_token = login(team, "team")
    resp = team.get("/api/v1/matches", **auth(team_token))
    check("越权 403/FORBIDDEN",
          resp.status_code == 403 and resp.json()["code"] == "FORBIDDEN",
          f"status={resp.status_code} code={resp.json().get('code')}")

    # 5) 409：PUT 带错 version
    upd = dict(payload)
    upd["version"] = 999
    resp = admin.put(f"/api/v1/matches/{match_id}", data=json.dumps(upd),
                     content_type="application/json", **auth(admin_token))
    check("版本冲突 409/VERSION_CONFLICT",
          resp.status_code == 409 and resp.json()["code"] == "VERSION_CONFLICT",
          f"status={resp.status_code}")

    # 6) 正确版本更新（链式取 version）
    upd["version"] = m["version"]
    upd["venue"] = "奥体中心2号场"
    resp = admin.put(f"/api/v1/matches/{match_id}", data=json.dumps(upd),
                     content_type="application/json", **auth(admin_token))
    m = resp.json()["data"]
    check("正确版本更新 200 且版本递增",
          resp.status_code == 200 and m["venue"] == "奥体中心2号场" and m["version"] == 1,
          f"version={m['version']}")

    # 7) 发布
    resp = admin.post(f"/api/v1/matches/{match_id}/publish",
                      data=json.dumps({"version": m["version"]}),
                      content_type="application/json", **auth(admin_token))
    m = resp.json()["data"]
    check("发布 published=true", resp.status_code == 200 and m["published"] is True,
          f"published={m['published']}")

    # 8) 下线
    resp = admin.post(f"/api/v1/matches/{match_id}/unpublish",
                      data=json.dumps({"version": m["version"], "reason": "赛程调整"}),
                      content_type="application/json", **auth(admin_token))
    m = resp.json()["data"]
    check("下线 published=false", resp.status_code == 200 and m["published"] is False,
          f"published={m['published']}")

    # 9) 400 未知字段
    bad = dict(payload)
    bad["unexpectedField"] = "x"
    resp = admin.post("/api/v1/matches", data=json.dumps(bad),
                      content_type="application/json", **auth(admin_token))
    check("未知字段 400/INVALID_ARGUMENT",
          resp.status_code == 400 and resp.json()["code"] == "INVALID_ARGUMENT",
          f"status={resp.status_code}")

    # 10) 主客队相同 422
    same = dict(payload)
    same["awayTeamId"] = "team_a"
    resp = admin.post("/api/v1/matches", data=json.dumps(same),
                      content_type="application/json", **auth(admin_token))
    check("主客队相同 422/BUSINESS_VALIDATION_FAILED",
          resp.status_code == 422 and resp.json()["code"] == "BUSINESS_VALIDATION_FAILED",
          f"status={resp.status_code} code={resp.json().get('code')}")

    # 11) 删除（带当前版本）ActionResult
    cur = admin.get(f"/api/v1/matches/{match_id}", **auth(admin_token)).json()["data"]
    resp = admin.delete(f"/api/v1/matches/{match_id}?version={cur['version']}", **auth(admin_token))
    check("删除 ActionResult",
          resp.status_code == 200 and resp.json()["data"]["success"] is True,
          f"status={resp.status_code}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())