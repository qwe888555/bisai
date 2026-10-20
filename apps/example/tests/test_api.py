"""Example API 端点测试."""

import json

from django.test import Client


class TestCreateTagEndpoint:
    """POST /api/example/tags 创建标签."""

    def test_create_success(self, client: Client, tag_payload: dict) -> None:
        """201 Created."""
        resp = client.post(
            "/api/example/tags",
            data=json.dumps(tag_payload),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == tag_payload["name"]

    def test_invalid(self, client: Client) -> None:
        """缺少名称 → 422."""
        resp = client.post(
            "/api/example/tags",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 422


class TestListTagEndpoint:
    """GET /api/example/tags 标签列表."""

    def test_list(self, client: Client, sample_tag) -> None:
        """200 OK."""
        resp = client.get("/api/example/tags")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1
