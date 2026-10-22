"""T5-04 图文直播(5.4)+赛事文件(5.3)冒烟：文档挂载/过滤、直播帖feed游标、改删、直播链接URL校验。"""

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
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def login(client, username):
    r = client.post("/api/v1/auth/login", data=json.dumps(
        {"username": username, "password": "Foot@2026"}), content_type="application/json")
    return r.json()["data"]["accessToken"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def main() -> int:
    c = Client(); token = login(c, "admin")
    ev = c.post("/api/v1/events", data=json.dumps({
        "name": "T5-04 直播联赛", "season": "2026", "eventCategory": "LEAGUE",
        "registrationOpensAt": "2026-09-01T00:00:00+08:00",
        "registrationClosesAt": "2026-09-20T23:59:59+08:00",
        "status": "DRAFT"}), content_type="application/json", **auth(token))
    event_id = ev.json()["data"]["id"]
    gr = c.post(f"/api/v1/events/{event_id}/groups", data=json.dumps({
        "name": "小学甲组", "category": "PRIMARY", "gender": "MALE",
        "quota": 0, "maxPlayers": 11, "requiredMaterialTypes": ["PLAYER_CARD"]}),
        content_type="application/json", **auth(token))
    group_id = gr.json()["data"]["id"]
    mc = c.post("/api/v1/matches", data=json.dumps({
        "eventId": event_id, "groupId": group_id,
        "homeTeamId": "team_a", "awayTeamId": "team_b",
        "kickoffAt": "2026-10-26T15:00:00+08:00", "venue": "场1",
        "status": "SCHEDULED", "referees": []}),
        content_type="application/json", **auth(token))
    match_id = mc.json()["data"]["id"]

    # 5.3 赛事文件
    r = c.post(f"/api/v1/matches/{match_id}/documents", data=json.dumps({
        "documentType": "SIGN_IN", "fileId": "file_sign_001"}),
        content_type="application/json", **auth(token))
    check("挂载签到表 200", r.status_code == 200 and r.json()["code"] == "OK",
          f"status={r.status_code}")
    c.post(f"/api/v1/matches/{match_id}/documents", data=json.dumps({
        "documentType": "REFEREE_REPORT", "fileId": "file_ref_001"}),
        content_type="application/json", **auth(token))
    r = c.get(f"/api/v1/matches/{match_id}/documents?documentType=SIGN_IN", **auth(token))
    items = r.json()["data"]["items"]
    check("文档按类型过滤命中1条", len(items) == 1 and items[0]["documentType"] == "SIGN_IN",
          f"n={len(items)}")
    r = c.post(f"/api/v1/matches/{match_id}/documents", data=json.dumps({
        "documentType": "BAD_TYPE", "fileId": "x"}),
        content_type="application/json", **auth(token))
    check("非法documentType 400", r.status_code == 400, f"status={r.status_code}")

    # 5.4 图文直播帖 + feed 游标递增
    p1 = c.post(f"/api/v1/matches/{match_id}/live-posts", data=json.dumps({
        "text": "开赛！", "minute": 0, "imageFileIds": ["img1"]}),
        content_type="application/json", **auth(token)).json()["data"]
    p2 = c.post(f"/api/v1/matches/{match_id}/live-posts", data=json.dumps({
        "text": "第30分钟进球", "minute": 30}),
        content_type="application/json", **auth(token)).json()["data"]
    check("首帖cursor=1", p1["cursor"] == 1, f"cursor={p1['cursor']}")
    check("次帖cursor=2递增", p2["cursor"] == 2, f"cursor={p2['cursor']}")
    r = c.get(f"/api/v1/matches/{match_id}/live-posts", **auth(token))
    lst = r.json()["data"]["items"]
    check("直播帖列表新帖在前", lst[0]["id"] == p2["id"] and len(lst) == 2, f"n={len(lst)}")
    r = c.get(f"/api/v1/matches/{match_id}/live-posts/{p1['id']}", **auth(token))
    check("直播帖详情", r.status_code == 200 and r.json()["data"]["text"] == "开赛！")

    # 修改直播帖（乐观锁）
    upd = c.put(f"/api/v1/matches/{match_id}/live-posts/{p1['id']}", data=json.dumps({
        "text": "开赛（更正）", "minute": 1, "version": p1["version"]}),
        content_type="application/json", **auth(token)).json()["data"]
    check("修改直播帖 200 版本+1", upd["version"] == 1 and upd["cursor"] > p1["cursor"])
    r = c.put(f"/api/v1/matches/{match_id}/live-posts/{p1['id']}", data=json.dumps({
        "text": "x", "version": 999}), content_type="application/json", **auth(token))
    check("修改版本错 409", r.status_code == 409, f"status={r.status_code}")

    # 删除直播帖
    r = c.delete(f"/api/v1/matches/{match_id}/live-posts/{p2['id']}?version={p2['version']}",
                 **auth(token))
    check("删除直播帖 ActionResult", r.status_code == 200 and r.json()["data"]["success"] is True)
    r = c.get(f"/api/v1/matches/{match_id}/live-posts", **auth(token))
    check("删除后列表剩1条", len(r.json()["data"]["items"]) == 1)

    # 5.4 直播链接
    r = c.post(f"/api/v1/matches/{match_id}/live-links", data=json.dumps({
        "title": "官方直播", "url": "https://live.example.com/1", "enabled": True}),
        content_type="application/json", **auth(token))
    check("创建直播链接 200", r.status_code == 200 and r.json()["code"] == "OK")
    link = r.json()["data"]
    r = c.post(f"/api/v1/matches/{match_id}/live-links", data=json.dumps({
        "title": "非法", "url": "ftp://x.com"}),
        content_type="application/json", **auth(token))
    check("非http(s)链接 422", r.status_code == 422, f"status={r.status_code}")
    r = c.get(f"/api/v1/matches/{match_id}/live-links/{link['id']}", **auth(token))
    check("直播链接详情", r.status_code == 200 and r.json()["data"]["enabled"] is True)
    r = c.put(f"/api/v1/matches/{match_id}/live-links/{link['id']}", data=json.dumps({
        "title": "官方直播(下)", "url": "https://live.example.com/2",
        "enabled": False, "version": link["version"]}),
        content_type="application/json", **auth(token))
    check("修改直播链接关闭", r.status_code == 200 and r.json()["data"]["enabled"] is False)
    r = c.delete(f"/api/v1/matches/{match_id}/live-links/{link['id']}?version=1", **auth(token))
    check("删除直播链接 ActionResult", r.status_code == 200 and r.json()["data"]["success"] is True)

    # 403
    team = Client(); tt = login(team, "team")
    r = team.get(f"/api/v1/matches/{match_id}/live-posts", **auth(tt))
    check("越权 403", r.status_code == 403 and r.json()["code"] == "FORBIDDEN")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)}: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())