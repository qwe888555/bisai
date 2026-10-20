"""服务层单元测试."""

import pytest

from api.exceptions import ItemNotFoundError
from api.models import Item
from api.schemas import ItemCreate, ItemUpdate
from api.services import create_item, delete_item, get_item, list_items, update_item


class TestCreateItem:
    """创建商品服务测试."""

    def test_create_with_valid_data(self, db: None) -> None:
        """有效数据创建."""
        payload = ItemCreate(title="新品", price=50.00)
        item = create_item(payload)
        assert item.title == "新品"
        assert float(item.price) == 50.00

    def test_create_with_defaults(self, db: None) -> None:
        """默认状态为 draft."""
        payload = ItemCreate(title="默认状态", price=10.00)
        item = create_item(payload)
        assert item.status == "draft"
        assert item.description == ""


class TestGetItem:
    """获取商品服务测试."""

    def test_get_existing(self, db: None, sample_item: Item) -> None:
        """获取存在的商品."""
        item = get_item(sample_item.id)
        assert item.id == sample_item.id
        assert item.title == sample_item.title

    def test_get_nonexistent(self, db: None) -> None:
        """获取不存在的商品应抛出异常."""
        with pytest.raises(ItemNotFoundError):
            get_item(99999)


class TestListItem:
    """商品列表服务测试."""

    def test_list_all(self, db: None, sample_item: Item) -> None:
        """返回分页结果."""
        items, total = list_items(page=1, per_page=20)
        assert total >= 1
        assert len(items) >= 1

    def test_list_with_status_filter(self, db: None, sample_item: Item) -> None:
        """按状态筛选."""
        items, total = list_items(status="published")
        assert total >= 1
        assert all(item.status == "published" for item in items)

    def test_list_empty_status(self, db: None) -> None:
        """不存在的状态返回空."""
        items, total = list_items(status="nonexistent")
        assert total == 0
        assert items == []


class TestUpdateItem:
    """更新商品服务测试."""

    def test_partial_update(self, db: None, sample_item: Item) -> None:
        """部分更新 — 只更新提供字段."""
        payload = ItemUpdate(title="新标题")
        item = update_item(sample_item.id, payload)
        assert item.title == "新标题"
        assert item.description == sample_item.description  # 未改变

    def test_update_nonexistent(self, db: None) -> None:
        """更新不存在的商品应抛出异常."""
        with pytest.raises(ItemNotFoundError):
            update_item(99999, ItemUpdate(title="无"))


class TestDeleteItem:
    """删除商品服务测试."""

    def test_soft_delete(self, db: None, sample_item: Item) -> None:
        """软删除标记."""
        delete_item(sample_item.id)
        item = Item.objects.get(id=sample_item.id)
        assert item.is_deleted is True
        assert item.deleted_at is not None

    def test_delete_then_hidden_from_list(self, db: None, sample_item: Item) -> None:
        """软删除后列表不再返回."""
        delete_item(sample_item.id)
        items, total = list_items()
        assert sample_item.id not in [i.id for i in items]
