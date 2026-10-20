#!/usr/bin/env python
"""一键创建新的业务模块 (App).

用法:
    uv run python scaffold_app.py <模块名>

示例:
    uv run python scaffold_app.py orders
    uv run python scaffold_app.py users

执行流程:
    1. 创建 apps/<模块名>/ 目录结构和所有文件
    2. 注册到 config/settings.py 的 LOCAL_APPS
    3. 注册路由到 config/urls.py
    4. 输出下一步操作
"""

import re
import sys
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent
APPS_DIR = BASE_DIR / "apps"
SETTINGS_FILE = BASE_DIR / "config" / "settings.py"
URLS_FILE = BASE_DIR / "config" / "urls.py"

# ─── 文件模板 ────────────────────────────────────────────────────

APPS_PY_TEMPLATE = '''"""{{APP_NAME}} 应用配置."""
from django.apps import AppConfig


class {{AppName}}Config(AppConfig):
    """{{APP_VERBOSE_NAME}}模块配置."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.{{app_name}}"
    verbose_name = "{{APP_VERBOSE_NAME}}模块"
'''

MODELS_PY_TEMPLATE = '''"""{{APP_NAME}} 数据模型."""
from django.db import models

from common.base.models import TimestampMixin


class {{ModelName}}(TimestampMixin):
    """{{MODEL_VERBOSE_NAME}}模型."""

    name = models.CharField(max_length=100, verbose_name="名称")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = "apps_{{app_name}}_{{model_table}}"
        verbose_name = "{{MODEL_VERBOSE_NAME}}"
        verbose_name_plural = "{{MODEL_VERBOSE_NAME}}"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name
'''

SCHEMAS_PY_TEMPLATE = '''"""{{APP_NAME}} Pydantic Schema."""
from datetime import datetime

from ninja import Schema
from pydantic import Field


class {{ModelName}}Create(Schema):
    """创建请求."""
    name: str = Field(..., min_length=1, max_length=100, examples=["示例"])
    is_active: bool = True


class {{ModelName}}Update(Schema):
    """更新请求."""
    name: str | None = Field(None, min_length=1, max_length=100)
    is_active: bool | None = None


class {{ModelName}}Response(Schema):
    """响应."""
    id: int
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
'''

SERVICES_PY_TEMPLATE = '''"""{{APP_NAME}} 业务逻辑层."""
import math

from django.db import transaction

from apps.{{app_name}}.models import {{ModelName}}
from apps.{{app_name}}.schemas import {{ModelName}}Create, {{ModelName}}Update
from common.base.exceptions import BusinessError


class {{ModelName}}NotFoundError(BusinessError):
    """{{MODEL_VERBOSE_NAME}}不存在异常."""

    def __init__(self, {{model_id}}: int) -> None:
        super().__init__(message=f"{{MODEL_VERBOSE_NAME}}不存在: {{{{{model_id}}}}}", code="{{app_name}}_{{model_table}}_not_found")


@transaction.atomic
def create_{{model_table}}(payload: {{ModelName}}Create) -> {{ModelName}}:
    """创建."""
    return {{ModelName}}.objects.create(**payload.model_dump())


def get_{{model_table}}({{model_id}}: int) -> {{ModelName}}:
    """获取."""
    try:
        return {{ModelName}}.objects.get(id={{model_id}})
    except {{ModelName}}.DoesNotExist as err:
        raise {{ModelName}}NotFoundError({{model_id}}) from err


def list_{{model_table}}s(page: int = 1, per_page: int = 20) -> tuple[list[{{ModelName}}], int]:
    """列表（分页）. """
    qs = {{ModelName}}.objects.filter(is_active=True).order_by("-created_at")
    total = qs.count()
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages)) if total > 0 else 1
    offset = (safe_page - 1) * per_page
    return list(qs[offset : offset + per_page]), total


@transaction.atomic
def update_{{model_table}}({{model_id}}: int, payload: {{ModelName}}Update) -> {{ModelName}}:
    """更新."""
    {{model_name}} = get_{{model_table}}({{model_id}})
    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        for field, value in update_data.items():
            setattr({{model_name}}, field, value)
        {{model_name}}.save()
    return {{model_name}}


@transaction.atomic
def delete_{{model_table}}({{model_id}}: int) -> None:
    """删除."""
    {{model_name}} = get_{{model_table}}({{model_id}})
    {{model_name}}.delete()
'''

API_PY_TEMPLATE = '''"""{{APP_NAME}} API 路由."""
import math
from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.{{app_name}}.schemas import {{ModelName}}Create, {{ModelName}}Response, {{ModelName}}Update
from apps.{{app_name}}.services import create_{{model_table}}, delete_{{model_table}}, get_{{model_table}}, list_{{model_table}}s, update_{{model_table}}
from common.base.schemas import PaginatedResponse

router = Router(tags=["{{APP_TITLE}}"])


@router.post("/{{model_table}}s", response={201: {{ModelName}}Response}, summary="创建")
def create_endpoint(request: HttpRequest, payload: {{ModelName}}Create) -> Any:
    """创建（状态码 201）. """
    obj = create_{{model_table}}(payload)
    return 201, obj


@router.get("/{{model_table}}s", response=PaginatedResponse[{{ModelName}}Response], summary="列表")
def list_endpoint(
    request: HttpRequest,
    page: int = 1,
    per_page: int = 20,
) -> Any:
    """分页查询列表."""
    items, total = list_{{model_table}}s(page=page, per_page=per_page)
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, math.ceil(total / per_page)),
    }


@router.get("/{{model_table}}s/{{{model_id}}}", response={{ModelName}}Response, summary="详情")
def get_endpoint(request: HttpRequest, {{model_id}}: int) -> Any:
    """获取单个."""
    return get_{{model_table}}({{model_id}})


@router.patch("/{{model_table}}s/{{{model_id}}}", response={{ModelName}}Response, summary="更新")
def update_endpoint(request: HttpRequest, {{model_id}}: int, payload: {{ModelName}}Update) -> Any:
    """更新信息."""
    return update_{{model_table}}({{model_id}}, payload)


@router.delete("/{{model_table}}s/{{{model_id}}}", response={204: None}, summary="删除")
def delete_endpoint(request: HttpRequest, {{model_id}}: int) -> Any:
    """删除（状态码 204）. """
    delete_{{model_table}}({{model_id}})
    return 204, None
'''

ADMIN_PY_TEMPLATE = '''"""{{APP_NAME}} Admin 配置."""
from django.contrib import admin

from apps.{{app_name}}.models import {{ModelName}}


@admin.register({{ModelName}})
class {{ModelName}}Admin(admin.ModelAdmin):
    """管理."""
    list_display = ["id", "name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    ordering = ["-created_at"]
'''

INIT_PY = ""

TEST_CONFTEST_TEMPLATE = '''"""{{APP_NAME}} 测试夹具."""
import pytest
from django.test import Client

from apps.{{app_name}}.models import {{ModelName}}


@pytest.fixture
def client(db: None) -> Client:
    """Django 测试客户端."""
    return Client()


@pytest.fixture
def {{model_table}}_payload() -> dict:
    """创建请求数据."""
    return {"name": "测试", "is_active": True}


@pytest.fixture
def sample_{{model_table}}(db: None) -> {{ModelName}}:
    """预先创建的实例."""
    return {{ModelName}}.objects.create(name="参考", is_active=True)
'''

TEST_MODELS_TEMPLATE = '''"""{{APP_NAME}} 模型测试."""
from apps.{{app_name}}.models import {{ModelName}}


class Test{{ModelName}}Model:
    """模型测试."""

    def test_create(self, db: None) -> None:
        """创建正常."""
        obj = {{ModelName}}.objects.create(name="测试")
        assert obj.id is not None
        assert obj.name == "测试"
'''

TEST_SERVICES_TEMPLATE = '''"""{{APP_NAME}} 服务层测试."""
import pytest

from apps.{{app_name}}.models import {{ModelName}}
from apps.{{app_name}}.schemas import {{ModelName}}Create, {{ModelName}}Update
from apps.{{app_name}}.services import (
    {{ModelName}}NotFoundError,
    create_{{model_table}},
    delete_{{model_table}},
    get_{{model_table}},
    list_{{model_table}}s,
    update_{{model_table}},
)


class TestCreate:
    """创建服务测试."""

    def test_valid(self, db: None) -> None:
        """有效数据创建."""
        payload = {{ModelName}}Create(name="测试")
        obj = create_{{model_table}}(payload)
        assert obj.name == "测试"


class TestGet:
    """获取服务测试."""

    def test_not_found(self, db: None) -> None:
        """不存在抛出异常."""
        with pytest.raises({{ModelName}}NotFoundError):
            get_{{model_table}}(99999)
'''

TEST_API_TEMPLATE = '''"""{{APP_NAME}} API 端点测试."""
import json

from django.test import Client


class TestCreateEndpoint:
    """POST /api/{{app_name}}/{{model_table}}s 创建."""

    def test_success(self, client: Client, {{model_table}}_payload: dict) -> None:
        """201 Created."""
        resp = client.post(
            "/api/{{app_name}}/{{model_table}}s",
            data=json.dumps({{model_table}}_payload),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == {{model_table}}_payload["name"]


class TestListEndpoint:
    """GET /api/{{app_name}}/{{model_table}}s 列表."""

    def test_list(self, client: Client, sample_{{model_table}}) -> None:
        """200 OK."""
        resp = client.get("/api/{{app_name}}/{{model_table}}s")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
'''

# ─── 工具函数 ────────────────────────────────────────────────────


def to_camel(name: str) -> str:
    """snake_case → CamelCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def render(template: str, **kwargs: str) -> str:
    """简易模板渲染（替换 {{key}} 占位符）."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{{" + key + "}}", value)
    # 清理未替换的占位符
    result = re.sub(r"\{\{[^}]+\}\}", "", result)
    return result


def ensure_file_has_line(filepath: Path, line: str, marker: str) -> bool:
    """在文件中 marker 位置后插入一行，如果不存在的话。返回是否插入."""
    content = filepath.read_text(encoding="utf-8")
    if line in content:
        return False
    if marker in content:
        new_content = content.replace(marker, marker + "\n" + line, 1)
        filepath.write_text(new_content, encoding="utf-8")
        return True
    return False


# ─── 主流程 ──────────────────────────────────────────────────────


def main(app_name: str) -> None:
    """主流程."""
    app_dir = APPS_DIR / app_name

    # ── 验证 ──────────────────────────────────────────────────
    if not re.match(r"^[a-z][a-z0-9_]*$", app_name):
        print(
            f"❌ 模块名 '{app_name}' 无效。请使用小写字母、数字和下划线，且以字母开头。"
        )
        sys.exit(1)

    if app_dir.exists():
        print(f"❌ 模块 '{app_name}' 已存在: {app_dir}")
        sys.exit(1)

    # ── 模板变量 ──────────────────────────────────────────────
    app_title = to_camel(app_name)
    model_name_single = app_name.rstrip("s")
    model_name = to_camel(model_name_single)
    model_table = model_name_single
    model_id = model_name_single + "_id"

    ctx = {
        "app_name": app_name,
        "APP_NAME": app_title,
        "APP_TITLE": app_title,
        "APP_VERBOSE_NAME": app_title,
        "ModelName": model_name,
        "model_name": model_name_single,
        "model_table": model_table,
        "model_id": model_id,
        "MODEL_VERBOSE_NAME": app_title,
    }

    # ── 创建目录 ──────────────────────────────────────────────
    (app_dir / "migrations").mkdir(parents=True, exist_ok=True)
    (app_dir / "tests").mkdir(exist_ok=True)

    # ── 生成文件 ──────────────────────────────────────────────
    files = {
        "__init__.py": INIT_PY,
        "apps.py": APPS_PY_TEMPLATE,
        "models.py": MODELS_PY_TEMPLATE,
        "schemas.py": SCHEMAS_PY_TEMPLATE,
        "services.py": SERVICES_PY_TEMPLATE,
        "api.py": API_PY_TEMPLATE,
        "admin.py": ADMIN_PY_TEMPLATE,
        "migrations/__init__.py": INIT_PY,
        "tests/__init__.py": INIT_PY,
        "tests/conftest.py": TEST_CONFTEST_TEMPLATE,
        "tests/test_models.py": TEST_MODELS_TEMPLATE,
        "tests/test_services.py": TEST_SERVICES_TEMPLATE,
        "tests/test_api.py": TEST_API_TEMPLATE,
    }

    for relpath, template in files.items():
        path = app_dir / relpath
        content = render(template, **ctx)
        path.write_text(content, encoding="utf-8")
        print(f"  ✓ 创建 {relpath}")

    # ── 注册 settings.py ──────────────────────────────────────
    marker = '"api",'
    insert_line = f'    "apps.{app_name}",'
    settings_path = SETTINGS_FILE
    content = settings_path.read_text(encoding="utf-8")
    if insert_line not in content:
        new_content = content.replace(marker, marker + "\n" + insert_line, 1)
        settings_path.write_text(new_content, encoding="utf-8")
        print("  ✓ 注册到 settings.py (LOCAL_APPS)")

    # ── 注册 urls.py ──────────────────────────────────────────
    urls_path = URLS_FILE
    content = urls_path.read_text(encoding="utf-8")

    # 添加 import
    import_line = f"from apps.{app_name}.api import router as {app_name}_router"
    if import_line not in content:
        # 在最后一个 import 之后插入
        import_marker = "from common.base.exceptions import BusinessError"
        if import_marker in content:
            content = content.replace(import_marker, import_marker + "\n" + import_line)

    # 添加 add_router
    router_line = f'api.add_router("/{app_name}/", {app_name}_router)'
    if router_line not in content:
        # 在最后一个 add_router 之后插入
        # 查找 add_router 行并追加
        lines = content.split("\n")
        last_router_idx = -1
        for i, line in enumerate(lines):
            if "api.add_router(" in line:
                last_router_idx = i
        if last_router_idx >= 0:
            lines.insert(last_router_idx + 1, router_line)
            content = "\n".join(lines)

    urls_path.write_text(content, encoding="utf-8")
    print("  ✓ 注册到 urls.py (路由)")

    # ── 完成 ──────────────────────────────────────────────────
    print(f"""
✅ 新模块 '{app_name}' 创建完成！
   路径: {app_dir}

下一步:
  uv run python manage.py makemigrations {model_name_single}
  uv run python manage.py migrate {model_name_single}
  uv run pytest apps/{app_name}/tests/ -v
  uv run ruff check apps/{app_name}/
""")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
