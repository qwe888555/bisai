"""Example Admin 配置."""

from django.contrib import admin

from apps.example.models import ExampleTag


@admin.register(ExampleTag)
class ExampleTagAdmin(admin.ModelAdmin):
    """标签管理."""

    list_display = ["id", "name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    ordering = ["-created_at"]
