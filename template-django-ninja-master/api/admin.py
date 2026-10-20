"""Django Admin 配置.

注册数据模型到 Admin 后台，展示自定义管理界面配置。
"""

from django.contrib import admin

from api.models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    """商品管理."""

    list_display = ["id", "title", "status", "price", "is_deleted", "created_at"]
    list_display_links = ["id", "title"]
    list_filter = ["status", "is_deleted"]
    search_fields = ["title", "description"]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"
    readonly_fields = ["created_at", "updated_at", "deleted_at"]

    actions = ["soft_delete_selected", "restore_selected"]

    @admin.action(description="软删除选中的商品")
    def soft_delete_selected(self, request, queryset):
        """批量软删除."""
        for item in queryset:
            item.soft_delete()
        self.message_user(request, f"已软删除 {queryset.count()} 条记录")

    @admin.action(description="恢复选中的商品")
    def restore_selected(self, request, queryset):
        """批量恢复."""
        for item in queryset:
            item.restore()
        self.message_user(request, f"已恢复 {queryset.count()} 条记录")
