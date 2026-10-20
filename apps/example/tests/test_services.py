"""Example 服务层测试."""

import pytest

from apps.example.models import ExampleTag
from apps.example.schemas import TagCreate, TagUpdate
from apps.example.services import (
    TagNotFoundError,
    create_tag,
    delete_tag,
    get_tag,
    update_tag,
)


class TestCreateTag:
    """创建标签服务测试."""

    def test_create_valid(self, db: None) -> None:
        """有效数据创建."""
        payload = TagCreate(name="新品标签")
        tag = create_tag(payload)
        assert tag.name == "新品标签"
        assert tag.is_active is True


class TestGetTag:
    """获取标签服务测试."""

    def test_get_existing(self, db: None, sample_tag: ExampleTag) -> None:
        """获取存在的标签."""
        tag = get_tag(sample_tag.id)
        assert tag.id == sample_tag.id

    def test_get_not_found(self, db: None) -> None:
        """不存在的标签抛出异常."""
        with pytest.raises(TagNotFoundError):
            get_tag(99999)


class TestUpdateTag:
    """更新标签服务测试."""

    def test_partial_update(self, db: None, sample_tag: ExampleTag) -> None:
        """部分更新."""
        payload = TagUpdate(name="新名称")
        tag = update_tag(sample_tag.id, payload)
        assert tag.name == "新名称"
        assert tag.is_active is True  # 未改变


class TestDeleteTag:
    """删除标签服务测试."""

    def test_delete(self, db: None, sample_tag: ExampleTag) -> None:
        """物理删除."""
        delete_tag(sample_tag.id)
        assert ExampleTag.objects.filter(id=sample_tag.id).count() == 0
