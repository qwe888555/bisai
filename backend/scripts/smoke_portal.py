"""T5-07 门户公开读冒烟：公开对阵/详情/live-feed增量/live-links仅enabled/公开进球榜只算已发布。"""

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


def main():
    admin = Client(); token = login(admin, "admin")
    pub = Client()

    ev = admin.post("/api/v1/events", data=json.dumps({
        "name": "T5-07 门户联调", "season": "2026", "eventCategory": "LEAGUE",
        "registrationOpensAt": "2026-09-01T00:00:00+08:00",
        "registrationClosesAt": "2026-09-20T23:59:59+08:00",
        "status": "DRAFT", "description": "portal",
    }), content_type="application/json", **auth(token)).json()["data"]
    gr = admin.post(f"/api/v1/events/{ev['id']}/groups", data=json.dumps({
        "name": "小学甲组", "category": "PRIMARY", "gender": "MALE",
        "quota": 0, "maxPlayers": 11, "requiredMaterialTypes": [],
    }), content_type="application/json", **auth(token)).json()["data"]
    mc = admin.post("/api/v1/matches", data=json.dumps({
        "eventId": ev["id"], "groupId": gr["id"],
        "homeTeamId": "team_h", "awayTeamId": "team_a",
        "kickoffAt": "2026-10-28T15:00:00+08:00", "venue": "场9",
        "stage": "小组赛", "status": "FINISHED", "referees": [],
    }), content_type="application/json", **auth(token)).json()["data"]
    mid = mc["id"]

    # 0) 未发布对阵：公开 404 / 列表不含
    r = pub.get(f"/api/v1/public/matches/{mid}")
    check("未发布对阵公开 404", r.status_code == 404, f"status={r.status_code}")
    r = pub.get(f"/api/v1/public/matches?eventId={ev['id']}")
    check("未发布对阵不在公开列表", all(x["id"] != mid for x in r.json()["data"]["items"]))

    # 管理端发布对阵
    admin.post(f"/api/v1/matches/{mid}/publish", data=json.dumps({"version": mc["version"]}),
               content_type="application/json", **auth(token))

    # 1) 发布后公开可见，且不含内部 version
    r = pub.get(f"/api/v1/public/matches/{mid}")
    d = r.json()["data"]
    check("发布后公开详情 200/无version", r.status_code == 200 and "version" not in d
          and d["homeTeamName"] == "team_h" and d["status"] == "FINISHED", f"{d}")
    r = pub.get(f"/api/v1/public/matches?eventId={ev['id']}")
    check("公开列表含已发布对阵", any(x["id"] == mid for x in r.json()["data"]["items"]))

    # 2) live-feed：录2条帖，afterRevision=0 返回全部；增量空
    admin.post(f"/api/v1/matches/{mid}/live-posts", data=json.dumps(
        {"text": "开场"}), content_type="application/json", **auth(token))
    admin.post(f"/api/v1/matches/{mid}/live-posts", data=json.dumps(
        {"text": "进球了", "minute": 30}), content_type="application/json", **auth(token))
    r = pub.get(f"/api/v1/public/matches/{mid}/live-feed?afterRevision=0")
    feed = r.json()["data"]
    check("live-feed返回2帖+feedRevision", len(feed["items"]) == 2 and feed["feedRevision"] >= 2
          and feed["resetRequired"] is False, f"rev={feed['feedRevision']} n={len(feed['items'])}")
    r = pub.get(f"/api/v1/public/matches/{mid}/live-feed?afterRevision={feed['feedRevision']}")
    check("增量轮询空items", len(r.json()["data"]["items"]) == 0)

    # 3) live-links：enabled/disabled 各一，公开只含 enabled
    admin.post(f"/api/v1/matches/{mid}/live-links", data=json.dumps(
        {"title": "直播一", "url": "https://a.com/live", "enabled": True}),
        content_type="application/json", **auth(token))
    admin.post(f"/api/v1/matches/{mid}/live-links", data=json.dumps(
        {"title": "下架链接", "url": "https://b.com/live", "enabled": False}),
        content_type="application/json", **auth(token))
    r = pub.get(f"/api/v1/public/matches/{mid}/live-links")
    links = r.json()["data"]["items"]
    check("公开links只含enabled", len(links) == 1 and links[0]["title"] == "直播一"
          and "version" not in links[0], f"{links}")

    # 4) 录进球后公开进球榜只统计已发布对阵
    def goal(team, scorer, seq):
        m = admin.get(f"/api/v1/matches/{mid}", **auth(token)).json()["data"]
        h = auth(token); h["HTTP_IDEMPOTENCY_KEY"] = f"t507-goal-{seq:04d}-abcdef"
        admin.post(f"/api/v1/matches/{mid}/events", data=json.dumps({
            "eventType": "GOAL", "minute": seq, "teamId": team,
            "matchVersion": m["version"], "scorerAthleteId": "ath_pub_1"}),
            content_type="application/json", **h)
    goal("team_h", "ath_pub_1", 1)
    goal("team_h", "ath_pub_1", 2)
    r = pub.get(f"/api/v1/public/statistics/goals?eventId={ev['id']}")
    rows = {x["athleteId"]: x for x in r.json()["data"]["items"]}
    check("公开进球榜含已发布对阵进球", rows.get("ath_pub_1", {}).get("goals") == 2,
          f"{r.json()['data']['items']}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)}: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())