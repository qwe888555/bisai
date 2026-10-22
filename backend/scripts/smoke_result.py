"""T5-06 赛果(5.5)+进球榜冒烟：NORMAL事件派生/FORFEIT人工裁定/发布/撤下/密集排名/乌龙不计/越权。"""

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


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def login(c, u):
    r = c.post("/api/v1/auth/login", data=json.dumps(
        {"username": u, "password": "Foot@2026"}), content_type="application/json")
    return r.json()["data"]["accessToken"]


def auth(t):
    return {"HTTP_AUTHORIZATION": f"Bearer {t}"}


def make_event_match(admin, token, status="FINISHED", category="PRIMARY"):
    ev = admin.post("/api/v1/events", data=json.dumps({
        "name": "T5-06 赛果联赛", "season": "2026", "eventCategory": "LEAGUE",
        "registrationOpensAt": "2026-09-01T00:00:00+08:00",
        "registrationClosesAt": "2026-09-20T23:59:59+08:00",
        "status": "DRAFT", "description": "赛果冒烟",
    }), content_type="application/json", **auth(token)).json()["data"]
    gr = admin.post(f"/api/v1/events/{ev['id']}/groups", data=json.dumps({
        "name": "小学甲组", "category": category, "gender": "MALE",
        "quota": 0, "maxPlayers": 11, "requiredMaterialTypes": ["PLAYER_CARD"],
    }), content_type="application/json", **auth(token)).json()["data"]
    mc = admin.post("/api/v1/matches", data=json.dumps({
        "eventId": ev["id"], "groupId": gr["id"],
        "homeTeamId": "team_a", "awayTeamId": "team_b",
        "kickoffAt": "2026-10-27T15:00:00+08:00", "venue": "场1",
        "stage": "小组赛", "status": status, "referees": [],
    }), content_type="application/json", **auth(token)).json()["data"]
    return ev, gr, mc


_goal_seq = {"n": 0}


def add_goal(admin, token, match_id, team, scorer, minute, own=False, key=""):
    m = admin.get(f"/api/v1/matches/{match_id}", **auth(token)).json()["data"]
    body = {"eventType": "GOAL", "minute": minute, "teamId": team,
            "matchVersion": m["version"], "scorerAthleteId": scorer, "ownGoal": own}
    _goal_seq["n"] += 1
    h = auth(token)
    h["HTTP_IDEMPOTENCY_KEY"] = f"t506-goal-{_goal_seq['n']:04d}-abcdef1234"
    return admin.post(f"/api/v1/matches/{match_id}/events", data=json.dumps(body),
                      content_type="application/json", **h)


def main():
    admin = Client(); token = login(admin, "admin")
    ev, gr, mc = make_event_match(admin, token)
    match_id = mc["id"]

    # 录进球：ath_001 两球(team_a)，ath_002 一球(team_b)，乌龙ath_003不计榜
    add_goal(admin, token, match_id, "team_a", "ath_001", 10, key="g1")
    add_goal(admin, token, match_id, "team_a", "ath_001", 55, key="g2")
    add_goal(admin, token, match_id, "team_b", "ath_002", 33, key="g3")
    # 乌龙：team_b 得分，ath_003 属对方，个人榜不计
    add_goal(admin, token, match_id, "team_b", "ath_003", 70, own=True, key="g4")

    # 1) 初始赛果 NORMAL 由事件派生 2:2（乌龙计球队比分但不计个人榜）
    r = admin.get(f"/api/v1/matches/{match_id}/result", **auth(token))
    res = r.json()["data"]
    check("GET赛果 NORMAL 2:2 未发布", res["resultType"] == "NORMAL"
          and res["homeScore"] == 2 and res["awayScore"] == 2 and res["published"] is False,
          f"{res}")

    # 2) PUT NORMAL：忽略手填，仍事件派生
    r = admin.put(f"/api/v1/matches/{match_id}/result", data=json.dumps({
        "version": res["version"], "resultType": "NORMAL",
        "manualHomeScore": 9, "manualAwayScore": 9}),
        content_type="application/json", **auth(token))
    res = r.json()["data"]
    check("NORMAL忽略手填 仍2:2", res["homeScore"] == 2 and res["awayScore"] == 2
          and res["resultType"] == "NORMAL", f"{res}")

    # 3) recorder 弃权 → 403
    rec = Client(); rt = login(rec, "recorder")
    r = rec.put(f"/api/v1/matches/{match_id}/result", data=json.dumps({
        "version": res["version"], "resultType": "FORFEIT",
        "manualHomeScore": 3, "manualAwayScore": 0, "reason": "弃权"}),
        content_type="application/json", **auth(rt))
    check("录入员弃权 403", r.status_code == 403 and r.json()["code"] == "FORBIDDEN",
          f"status={r.status_code}")

    # 4) admin FORFEIT 缺 reason → 422
    r = admin.put(f"/api/v1/matches/{match_id}/result", data=json.dumps({
        "version": res["version"], "resultType": "FORFEIT",
        "manualHomeScore": 3, "manualAwayScore": 0}),
        content_type="application/json", **auth(token))
    check("弃权缺reason 422", r.status_code == 422, f"status={r.status_code}")

    # 5) admin FORFEIT 带 reason+人工分 → 3:0
    r = admin.put(f"/api/v1/matches/{match_id}/result", data=json.dumps({
        "version": res["version"], "resultType": "FORFEIT",
        "manualHomeScore": 3, "manualAwayScore": 0, "reason": "客队弃权"}),
        content_type="application/json", **auth(token))
    res = r.json()["data"]
    check("FORFEIT人工3:0+原因", res["resultType"] == "FORFEIT" and res["homeScore"] == 3
          and res["awayScore"] == 0 and res["reason"] == "客队弃权", f"{res}")

    # 6) 发布赛果（FINISHED）
    r = admin.post(f"/api/v1/matches/{match_id}/result/publish",
                   data=json.dumps({"version": res["version"]}),
                   content_type="application/json", **auth(token))
    res = r.json()["data"]
    check("发布赛果 published=true", r.status_code == 200 and res["published"] is True)

    # 7) 非FINISHED 发布 → 409
    _, _, scheduled = make_event_match(admin, token, status="SCHEDULED")
    r = admin.post(f"/api/v1/matches/{scheduled['id']}/result/publish",
                   data=json.dumps({"version": scheduled["version"]}),
                   content_type="application/json", **auth(token))
    check("未结束发布 409", r.status_code == 409 and r.json()["code"] == "STATE_CONFLICT",
          f"status={r.status_code}")

    # 8) DELETE 撤下：回 NORMAL 重算事件比分，不自动发布
    r = admin.delete(f"/api/v1/matches/{match_id}/result?version={res['version']}", **auth(token))
    res = r.json()["data"]
    check("撤下 ActionResult", r.status_code == 200 and res["success"] is True)
    r = admin.get(f"/api/v1/matches/{match_id}/result", **auth(token)).json()["data"]
    check("撤下回NORMAL 2:2 未发布", r["resultType"] == "NORMAL" and r["homeScore"] == 2
          and r["awayScore"] == 2 and r["published"] is False, f"{r}")

    # 9) 进球榜：ath_001=2 rank1，ath_002=1 rank2，乌龙ath_003不计
    r = admin.get(f"/api/v1/statistics/goals?eventId={ev['id']}", **auth(token))
    page = r.json()["data"]
    rows = {x["athleteId"]: x for x in page["items"]}
    check("榜单 ath_001=2 rank1", rows.get("ath_001", {}).get("goals") == 2
          and rows["ath_001"]["rank"] == 1, f"{page['items']}")
    check("榜单 ath_002=1 rank2", rows.get("ath_002", {}).get("goals") == 1
          and rows["ath_002"]["rank"] == 2)
    check("乌龙ath_003不计榜", "ath_003" not in rows)

    # 10) category 过滤：PRIMARY 命中，MIDDLE 空
    r = admin.get(f"/api/v1/statistics/goals?eventId={ev['id']}&category=PRIMARY", **auth(token))
    check("PRIMARY过滤命中", r.json()["data"]["total"] == 2)
    r = admin.get(f"/api/v1/statistics/goals?eventId={ev['id']}&category=MIDDLE", **auth(token))
    check("MIDDLE过滤为空", r.json()["data"]["total"] == 0)

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)}: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())