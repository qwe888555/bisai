"""pytest 共享夹具（fixtures）."""

from decimal import Decimal

import pytest
from django.test import Client

from api.models import Item


@pytest.fixture
def client(db: None) -> Client:
    """Django 测试客户端（自动初始化测试数据库）."""
    return Client()


@pytest.fixture
def item_payload() -> dict:
    """创建商品的请求数据."""
    return {
        "title": "测试商品",
        "description": "这是一个测试商品",
        "price": 99.99,
        "status": "draft",
    }


@pytest.fixture
def sample_item(db: None) -> Item:
    """预先创建的商品实例."""
    return Item.objects.create(
        title="样本书",
        description="示例商品描述",
        price=Decimal("199.00"),
        status="published",
    )
