"""T5-03 比赛事件流(5.2)冒烟：GOAL/PENALTY/SUBSTITUTION、重算比分、403/409/改/撤/幂等。

用法（backend 目录）：
    .venv/Scripts/python.exe scripts/smoke_match_events.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
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


def auth(token: str, key: str | None = None) -> dict:
    h = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    if key:
        h["HTTP_IDEMPOTENCY_KEY"] = key
    return h


def main() -> int:
    admin = Client()
    token = login(admin, "admin")

    # 前置赛事/组别/比赛
    ev = admin.post("/api/v1/events", data=json.dumps({
        "name": "T5-03 事件联赛", "season": "2026", "eventCategory": "LEAGUE",
        "registrationOpensAt": "2026-09-01T00:00:00+08:00",
        "registrationClosesAt": "2026-09-20T23:59:59+08:00",
        "status": "DRAFT", "description": "事件冒烟",
    }), content_type="application/json", **auth(token))
    event_id = ev.json()["data"]["id"]
    gr = admin.post(f"/api/v1/events/{event_id}/groups", data=json.dumps({
        "name": "小学甲组", "category": "PRIMARY", "gender": "MALE",
        "quota": 0, "maxPlayers": 11, "requiredMaterialTypes": ["PLAYER_CARD"],
    }), content_type="application/json", **auth(token))
    group_id = gr.json()["data"]["id"]
    mc = admin.post("/api/v1/matches", data=json.dumps({
        "eventId": event_id, "groupId": group_id,
        "homeTeamId": "team_a", "awayTeamId": "team_b",
        "kickoffAt": "2026-10-26T15:00:00+08:00", "venue": "场1", "stage": "小组赛",
        "status": "SCHEDULED", "referees": [],
    }), content_type="application/json", **auth(token))
    match = mc.json()["data"]
    match_id = match["id"]
    mver = match["version"]

    def fresh_match():
        return admin.get(f"/api/v1/matches/{match_id}", **auth(token)).json()["data"]

    # 1) 录入 GOAL 主队，带幂等键
    g1 = {
        "eventType": "GOAL", "minute": 12, "teamId": "team_a",
        "matchVersion": mver, "scorerAthleteId": "ath_001",
    }
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(g1),
                      content_type="application/json", **auth(token, "evt-key-one-123456"))
    b = resp.json()
    check("录入GOAL 200/OK", resp.status_code == 200 and b["code"] == "OK",
          f"status={resp.status_code} code={b.get('code')}")
    e1 = b["data"]
    check("GOAL带scorerAthleteId", e1.get("scorerAthleteId") == "ath_001")
    m = fresh_match()
    check("重算主队比分=1", m.get("homeScore") == 1 and m.get("awayScore") == 0,
          f"home={m.get('homeScore')} away={m.get('awayScore')} rev={m.get('scoreRevision')}")

    # 2) 幂等回放：同键同正文不重复计分
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(g1),
                      content_type="application/json", **auth(token, "evt-key-one-123456"))
    m = fresh_match()
    check("幂等回放不重复计分", resp.status_code == 200 and m.get("homeScore") == 1,
          f"home={m.get('homeScore')}")

    # 3) 录入 GOAL 客队，matchVersion 取最新
    m = fresh_match()
    g2 = {"eventType": "GOAL", "minute": 30, "teamId": "team_b",
          "matchVersion": m["version"], "scorerAthleteId": "ath_002"}
    admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(g2),
               content_type="application/json", **auth(token, "evt-key-two-123456"))
    m = fresh_match()
    check("客队进球后 1:1", m.get("homeScore") == 1 and m.get("awayScore") == 1,
          f"home={m.get('homeScore')} away={m.get('awayScore')}")

    # 4) 409：create 带错 matchVersion
    bad = dict(g2); bad["matchVersion"] = 999
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(bad),
                      content_type="application/json", **auth(token, "evt-key-x-123456"))
    check("录入版本冲突 409", resp.status_code == 409 and resp.json()["code"] == "VERSION_CONFLICT",
          f"status={resp.status_code}")

    # 5) 403：team 无权
    team = Client(); tt = login(team, "team")
    resp = team.get(f"/api/v1/matches/{match_id}/events", **auth(tt))
    check("越权 403/FORBIDDEN", resp.status_code == 403 and resp.json()["code"] == "FORBIDDEN")

    # 6) PENALTY 缺 reason → 422；正确 → 200
    m = fresh_match()
    pen_bad = {"eventType": "PENALTY", "minute": 40, "teamId": "team_a",
               "matchVersion": m["version"], "penaltyType": "YELLOW_CARD"}
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(pen_bad),
                       content_type="application/json", **auth(token, "evt-penbad-123456"))
    check("判罚缺reason 422", resp.status_code == 422 and resp.json()["code"] == "BUSINESS_VALIDATION_FAILED")
    pen = {"eventType": "PENALTY", "minute": 40, "teamId": "team_a",
           "matchVersion": m["version"], "penaltyType": "YELLOW_CARD", "reason": "危险铲球"}
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(pen),
                       content_type="application/json", **auth(token, "evt-pen-123456"))
    check("录入判罚 200", resp.status_code == 200 and resp.json()["code"] == "OK")

    # 7) SUBSTITUTION
    m = fresh_match()
    sub = {"eventType": "SUBSTITUTION", "minute": 55, "teamId": "team_a",
           "matchVersion": m["version"], "outAthleteId": "ath_10", "inAthleteId": "ath_11"}
    resp = admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(sub),
                      content_type="application/json", **auth(token, "evt-sub-123456"))
    check("录入换人 200", resp.status_code == 200 and resp.json()["code"] == "OK")

    # 8) 列表按 eventType 过滤 + 时间序列
    resp = admin.get(f"/api/v1/matches/{match_id}/events?eventType=GOAL", **auth(token))
    items = resp.json()["data"]["items"]
    check("GOAL过滤命中2条", len(items) == 2 and all(i["eventType"] == "GOAL" for i in items),
          f"n={len(items)}")
    minutes = [i["minute"] for i in items]
    check("按分钟升序", minutes == sorted(minutes), f"minutes={minutes}")

    # 9) 修改 GOAL（事件version + matchVersion 双校验）
    ev_detail = admin.get(f"/api/v1/matches/{match_id}/events/{e1['id']}", **auth(token)).json()["data"]
    m = fresh_match()
    upd = {"version": ev_detail["version"], "event": {
        "eventType": "GOAL", "minute": 15, "teamId": "team_a",
        "matchVersion": m["version"], "scorerAthleteId": "ath_001",
    }}
    resp = admin.put(f"/api/v1/matches/{match_id}/events/{e1['id']}", data=json.dumps(upd),
                     content_type="application/json", **auth(token))
    check("修改事件 200 且比分不变", resp.status_code == 200
          and fresh_match().get("homeScore") == 1, f"status={resp.status_code}")

    # 10) 撤销客队 GOAL → 比分回 1:0
    e2 = admin.get(f"/api/v1/matches/{match_id}/events?eventType=GOAL", **auth(token)).json()["data"]["items"]
    away_goal = next(i for i in e2 if i["teamId"] == "team_b")
    m = fresh_match()
    resp = admin.delete(
        f"/api/v1/matches/{match_id}/events/{away_goal['id']}?version={away_goal['version']}&matchVersion={m['version']}",
        **auth(token))
    check("撤销事件 ActionResult", resp.status_code == 200 and resp.json()["data"]["success"] is True)
    m = fresh_match()
    check("撤销后比分回 1:0", m.get("homeScore") == 1 and m.get("awayScore") == 0,
          f"home={m.get('homeScore')} away={m.get('awayScore')}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())