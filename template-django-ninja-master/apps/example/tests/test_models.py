"""Example 模型测试."""

from datetime import datetime

from apps.example.models import ExampleTag


class TestExampleTagModel:
    """ExampleTag 模型测试."""

    def test_create_tag(self, db: None) -> None:
        """创建正常标签."""
        tag = ExampleTag.objects.create(name="测试标签")
        assert tag.id is not None
        assert tag.name == "测试标签"
        assert tag.is_active is True

    def test_timestamp(self, db: None) -> None:
        """时间戳自动填充."""
        tag = ExampleTag.objects.create(name="时间测试")
        assert isinstance(tag.created_at, datetime)
        assert isinstance(tag.updated_at, datetime)

    def test_str(self, db: None) -> None:
        """__str__ 返回名称."""
        tag = ExampleTag.objects.create(name="名称测试")
        assert str(tag) == "名称测试"

    def test_unique_name(self, db: None) -> None:
        """名称唯一约束."""
        ExampleTag.objects.create(name="唯一标签")
        import pytest
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            ExampleTag.objects.create(name="唯一标签")
