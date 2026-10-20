"""数据模型单元测试."""

from datetime import datetime

from api.models import Item, ItemStatus


class TestItemModel:
    """Item 模型测试."""

    def test_create_item(self, db: None) -> None:
        """创建正常商品."""
        item = Item.objects.create(
            title="测试商品",
            description="描述",
            price=99.99,
            status=ItemStatus.DRAFT,
        )
        assert item.id is not None
        assert item.title == "测试商品"
        assert item.status == ItemStatus.DRAFT
        assert item.is_deleted is False
        assert item.deleted_at is None

    def test_timestamp_mixin(self, db: None) -> None:
        """时间戳自动填充."""
        item = Item.objects.create(title="时间测试")
        assert isinstance(item.created_at, datetime)
        assert isinstance(item.updated_at, datetime)

    def test_soft_delete(self, db: None) -> None:
        """软删除标记."""
        item = Item.objects.create(title="将被删除")
        item.soft_delete()
        assert item.is_deleted is True
        assert item.deleted_at is not None

    def test_restore(self, db: None) -> None:
        """恢复已删除商品."""
        item = Item.objects.create(title="被恢复")
        item.soft_delete()
        item.restore()
        assert item.is_deleted is False
        assert item.deleted_at is None

    def test_str_representation(self, db: None) -> None:
        """__str__ 返回标题."""
        item = Item.objects.create(title="字符串测试")
        assert str(item) == "字符串测试"

    def test_title_max_length_model(self, db: None) -> None:
        """标题超长时模型层面不会报错（SQLite 不强制 VARCHAR 长度）."""
        item = Item.objects.create(title="x" * 201)
        assert len(item.title) == 201
        # 注意: SQLite 不强制 max_length，生产环境使用 PostgreSQL/MySQL 时会截断或报错
