# 多 App 开发指南

## 快速创建新模块

```bash
uv run python scaffold_app.py <模块名>
```

示例：
```bash
uv run python scaffold_app.py orders
uv run python scaffold_app.py payments
uv run python scaffold_app.py articles
```

执行后自动生成：

```
apps/orders/
├── __init__.py
├── apps.py              # AppConfig（已注册到 settings）
├── models.py            # 继承 TimestampMixin 的空白模型
├── schemas.py           # Create/Update/Response Schema
├── services.py          # CRUD 业务逻辑骨架
├── api.py               # CRUD 端点 Router（已注册到 urls）
├── admin.py             # Admin 注册
├── migrations/
└── tests/               # 测试文件 + conftest
```

## 每个文件该写什么

### models.py

继承共享 Mixin，定义字段和 Meta：

```python
from django.db import models
from common.base.models import TimestampMixin

class Order(TimestampMixin):
    user_id = models.IntegerField(verbose_name="用户ID")
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=OrderStatus.choices)

    class Meta:
        db_table = "apps_order"  # 约定: apps_<模块名>
        ordering = ["-created_at"]
```

### schemas.py

```python
from ninja import Schema
from pydantic import Field

class OrderCreate(Schema):
    user_id: int = Field(..., gt=0)
    total: float = Field(..., gt=0)

class OrderResponse(Schema):
    id: int
    user_id: int
    total: float
    status: str
    created_at: datetime
```

### services.py

```python
from common.base.exceptions import BusinessError

class OrderNotFoundError(BusinessError):
    ...

def create_order(payload: OrderCreate) -> Order:
    ...

def get_order(order_id: int) -> Order:
    ...
```

### api.py

```python
from ninja import Router
router = Router(tags=["Orders"])
# ... 端点定义
```

## 路由注册

`config/urls.py` 中已自动注册：

```python
api.add_router("/orders/", orders_router)
```

访问路径：`/api/orders/orders`、`/api/orders/orders/{id}`

如需自定义前缀，手动修改 `config/urls.py` 即可。

## 跨 App 引用

### 引用另一个 App 的模型

```python
from apps.users.models import User

class Order(TimestampMixin):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
```

### 引用另一个 App 的服务

```python
from apps.users.services import get_user

def create_order(payload):
    user = get_user(payload.user_id)
    ...
```

### 循环引用预防

如果两个 App 相互引用，使用惰性导入：

```python
# 在函数内部导入，不在模块顶层导入
def get_order_with_user(order_id):
    from apps.users.services import get_user
    ...
```

## 常见问题

### Q: 迁移时报错 "No installed app with label 'xxx'"

Django 识别的 App 名称是 AppConfig 中 `name` 的最后一段：

```python
name = "apps.orders"  # Django 识别为 "orders"
```

迁移命令：
```bash
uv run python manage.py makemigrations orders
uv run python manage.py migrate orders
```

### Q: 如何删除一个 App？

```bash
# 1. 反向迁移
uv run python manage.py migrate orders zero

# 2. 删目录
rm -rf apps/orders

# 3. 从 settings.py 移除
# 4. 从 urls.py 移除
```
