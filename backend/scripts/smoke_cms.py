"""T5-05 内容引擎(7.1)+门户资源(7.2)+公开只读冒烟：XSS清洗/发布下线/资源校验/公开投影。"""

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
    c = Client(); token = login(c, "admin")

    # 1) 创建内容 + XSS 清洗
    evil = '<p>欢迎开赛</p><script>alert(1)</script><img src=x onerror=alert(2)>'
    r = c.post("/api/v1/contents", data=json.dumps({
        "contentType": "NEWS", "title": "开赛新闻", "summary": "摘要",
        "bodyHtml": evil, "pinned": True}),
        content_type="application/json", **auth(token))
    check("创建内容 200", r.status_code == 200 and r.json()["code"] == "OK", f"status={r.status_code}")
    news = r.json()["data"]
    check("XSS剥离script/onerror", "<script" not in news["bodyHtml"].lower()
          and "onerror" not in news["bodyHtml"].lower() and "<p>欢迎开赛</p>" in news["bodyHtml"],
          f"body={news['bodyHtml']}")
    check("bodyText为纯文本", "alert" not in news["bodyText"] and "欢迎开赛" in news["bodyText"])

    # 2) keyword 列表过滤
    r = c.get("/api/v1/contents?keyword=开赛", **auth(token))
    items = r.json()["data"]["items"]
    check("keyword过滤命中", len(items) >= 1 and items[0]["title"] == "开赛新闻")

    # 3) 修改 revision+1
    r = c.put(f"/api/v1/contents/{news['id']}", data=json.dumps({
        "contentType": "NEWS", "title": "开赛新闻(改)", "bodyHtml": "<p>更新正文</p>",
        "version": news["version"]}), content_type="application/json", **auth(token))
    upd = r.json()["data"]
    check("修改 revision=2/version=1", upd["revision"] == 2 and upd["version"] == 1,
          f"rev={upd['revision']} v={upd['version']}")

    # 4) 发布 → 公开可见
    r = c.post(f"/api/v1/contents/{news['id']}/publish", data=json.dumps({"version": upd["version"]}),
               content_type="application/json", **auth(token))
    check("发布 PUBLISHED", r.status_code == 200 and r.json()["data"]["status"] == "PUBLISHED")
    pub_ver = r.json()["data"]["version"]

    # 5) 下线
    r = c.post(f"/api/v1/contents/{news['id']}/unpublish", data=json.dumps(
        {"version": pub_ver, "reason": "撤稿"}), content_type="application/json", **auth(token))
    check("下线 OFFLINE", r.status_code == 200 and r.json()["data"]["status"] == "OFFLINE")
    off_ver = r.json()["data"]["version"]
    # 再发布供公开断言
    c.post(f"/api/v1/contents/{news['id']}/publish", data=json.dumps({"version": off_ver}),
           content_type="application/json", **auth(token))

    # 6) 草稿内容（公开不可见）
    draft = c.post("/api/v1/contents", data=json.dumps({
        "contentType": "POLICY", "title": "内部政策", "bodyHtml": "<p>内部</p>"}),
        content_type="application/json", **auth(token)).json()["data"]

    # 7) 资源校验
    r = c.post("/api/v1/portal-resources", data=json.dumps({
        "resourceType": "CAROUSEL", "title": "轮播1", "enabled": True}),
        content_type="application/json", **auth(token))
    check("轮播启用无图 422", r.status_code == 422, f"status={r.status_code}")
    r = c.post("/api/v1/portal-resources", data=json.dumps({
        "resourceType": "CAROUSEL", "title": "轮播1", "imageFileId": "img_a",
        "targetContentId": "c1", "targetUrl": "https://x.com"}),
        content_type="application/json", **auth(token))
    check("target二选一冲突 422", r.status_code == 422, f"status={r.status_code}")
    r = c.post("/api/v1/portal-resources", data=json.dumps({
        "resourceType": "CAROUSEL", "title": "轮播1", "imageFileId": "img_a",
        "targetUrl": "https://example.com/a", "sortOrder": 1, "enabled": True}),
        content_type="application/json", **auth(token))
    check("创建轮播 200", r.status_code == 200 and r.json()["code"] == "OK")
    carousel = r.json()["data"]
    r = c.post("/api/v1/portal-resources", data=json.dumps({
        "resourceType": "PARTNER", "title": "合作方", "targetUrl": "https://partner.com"}),
        content_type="application/json", **auth(token))
    check("创建合作方 200", r.status_code == 200)
    partner = r.json()["data"]
    # 关闭轮播
    r = c.put(f"/api/v1/portal-resources/{carousel['id']}", data=json.dumps({
        "resourceType": "CAROUSEL", "title": "轮播1", "imageFileId": "img_a",
        "targetUrl": "https://example.com/a", "sortOrder": 1,
        "enabled": False, "version": carousel["version"]}),
        content_type="application/json", **auth(token))
    check("关闭轮播 enabled=False", r.status_code == 200 and r.json()["data"]["enabled"] is False)

    # 8) 公开只读（无 token）
    pub = Client()
    r = pub.get("/api/v1/public/contents?contentType=NEWS")
    ids = [i["id"] for i in r.json()["data"]["items"]]
    check("公开只含已发布", news["id"] in ids and draft["id"] not in ids)
    r = pub.get(f"/api/v1/public/contents/{draft['id']}")
    check("草稿公开详情 404", r.status_code == 404, f"status={r.status_code}")
    r = pub.get(f"/api/v1/public/contents/{news['id']}")
    check("公开详情无内部字段", r.status_code == 200 and "version" not in r.json()["data"]
          and "status" not in r.json()["data"])
    r = pub.get("/api/v1/public/resources")
    res = r.json()["data"]["items"]
    check("公开资源只含enabled", all(x["id"] != carousel["id"] for x in res)
          and any(x["id"] == partner["id"] for x in res), f"n={len(res)}")

    # 9) 403：录入员无 CMS 权限
    rec = Client(); rt = login(rec, "recorder")
    r = rec.post("/api/v1/contents", data=json.dumps({
        "contentType": "NEWS", "title": "x", "bodyHtml": "<p>x</p>"}),
        content_type="application/json", **auth(rt))
    check("录入员越权 403", r.status_code == 403 and r.json()["code"] == "FORBIDDEN")

    # 10) 删除草稿 ActionResult
    r = c.delete(f"/api/v1/contents/{draft['id']}?version={draft['version']}", **auth(token))
    check("删除内容 ActionResult", r.status_code == 200 and r.json()["data"]["success"] is True)

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)}: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())