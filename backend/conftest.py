import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    """测试间清空缓存。

    限流计数器按 IP 存 Django cache（如 throttle:/api/v1/auth/login:127.0.0.1），
    同一进程内跨测试累积。全量跑测试套件（backend/tests + apps/*/tests）时，
    登录次数合计极易超过 THROTTLE_LOGIN 的 30/min，导致后续登录被 429 拦截。
    在测试前重置计数器，保证各测试的限流行为相互隔离。
    """
    cache.clear()
    yield
    cache.clear()
