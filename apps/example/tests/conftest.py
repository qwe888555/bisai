"""Example 测试夹具."""

import pytest
from django.test import Client

from apps.example.models import ExampleTag


@pytest.fixture
def client(db: None) -> Client:
    """Django 测试客户端."""
    return Client()


@pytest.fixture
def tag_payload() -> dict:
    """创建标签的请求数据."""
    return {"name": "测试标签", "is_active": True}


@pytest.fixture
def sample_tag(db: None) -> ExampleTag:
    """预先创建的标签实例."""
    return ExampleTag.objects.create(name="参考标签", is_active=True)
