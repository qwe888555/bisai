"""API 端点集成测试."""

import json

from django.test import Client


class TestCreateItemEndpoint:
    """POST /api/items 创建商品."""

    def test_create_success(self, client: Client, item_payload: dict) -> None:
        """201 Created."""
        resp = client.post(
            "/api/demo/items",
            data=json.dumps(item_payload),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == item_payload["title"]
        assert data["id"] > 0

    def test_create_missing_title(self, client: Client) -> None:
        """缺少必填字段 → 422."""
        resp = client.post(
            "/api/demo/items",
            data=json.dumps({"price": 10}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_create_empty_title(self, client: Client) -> None:
        """标题为空字符串 → 422."""
        resp = client.post(
            "/api/demo/items",
            data=json.dumps({"title": "", "price": 10}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_create_negative_price(self, client: Client) -> None:
        """价格为负 → 422."""
        resp = client.post(
            "/api/demo/items",
            data=json.dumps({"title": "测试", "price": -1}),
            content_type="application/json",
        )
        assert resp.status_code == 422


class TestListItemEndpoint:
    """GET /api/items 商品列表."""

    def test_list_success(self, client: Client, sample_item) -> None:
        """200 OK, 包含分页信息."""
        resp = client.get("/api/demo/items")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert data["total"] >= 1

    def test_list_with_status_filter(self, client: Client, sample_item) -> None:
        """按 status 筛选."""
        resp = client.get("/api/demo/items?status=published")
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["status"] == "published" for item in data["items"])


class TestGetItemEndpoint:
    """GET /api/items/{id} 商品详情."""

    def test_get_success(self, client: Client, sample_item) -> None:
        """200 OK."""
        resp = client.get(f"/api/demo/items/{sample_item.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sample_item.id
        assert data["title"] == sample_item.title

    def test_get_not_found(self, client: Client) -> None:
        """404."""
        resp = client.get("/api/demo/items/99999")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data


class TestUpdateItemEndpoint:
    """PATCH /api/items/{id} 更新商品."""

    def test_update_success(self, client: Client, sample_item) -> None:
        """200 OK, 字段部分更新."""
        resp = client.patch(
            f"/api/demo/items/{sample_item.id}",
            data=json.dumps({"title": "更新后标题"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "更新后标题"

    def test_update_not_found(self, client: Client) -> None:
        """404."""
        resp = client.patch(
            "/api/demo/items/99999",
            data=json.dumps({"title": "无"}),
            content_type="application/json",
        )
        assert resp.status_code == 404


class TestDeleteItemEndpoint:
    """DELETE /api/items/{id} 删除商品."""

    def test_delete_success(self, client: Client, sample_item) -> None:
        """204 No Content."""
        resp = client.delete(f"/api/demo/items/{sample_item.id}")
        assert resp.status_code == 204

    def test_delete_not_found(self, client: Client) -> None:
        """404."""
        resp = client.delete("/api/demo/items/99999")
        assert resp.status_code == 404


class TestHealthEndpoint:
    """GET /api/ 健康检查."""

    def test_health_check(self, client: Client) -> None:
        """200 OK."""
        resp = client.get("/api/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
