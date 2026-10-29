"""生产环境安全启动校验（B3）。

设计约定：
1. 仅当 DJANGO_ENV=prod 时执行强校验（由 apps.common.apps.CommonConfig.ready 触发）；
   dev/test 不校验，保证现有开发与测试运行方式零影响。
2. 本模块为纯函数：只读取传入的 settings 对象，不做 IO、不修改任何状态。
3. 错误信息只说明配置项与修复方向，绝不输出密钥值、数据库凭据或其他敏感环境变量。
4. 不提供任何绕过开关：校验不通过的 prod 环境必须修复配置后才能启动。

CRYPTO_KEY 门槛（临时告警策略）：
    人员敏感字段（person.id_card_cipher / phone_cipher）加解密业务逻辑上线前，
    本项必须由 warning 升级为 violation，并补充对应测试——见部署指南 §10.2。
"""

from __future__ import annotations

from typing import Any

# settings.py 中 SECRET_KEY 的已知开发默认值（仅用于比对识别，非泄露：该串本就公开在源码中）
DEV_DEFAULT_SECRET_KEY = "dev-only-change-me-please-32bytes-min"

# CORS_ORIGINS 的开发默认值（settings.py），生产沿用即告警
_DEV_DEFAULT_CORS = {"http://localhost:5173", "http://127.0.0.1:5173"}

VALID_ENVS = {"dev", "test", "prod"}


def assert_valid_env(value: str) -> None:
    """校验 DJANGO_ENV 取值；未知值显式报错，不静默回退 dev。

    报错不回显原始值（调用方输入可能包含任意内容）。
    """
    if value not in VALID_ENVS:
        raise ValueError(
            "DJANGO_ENV 取值非法：仅支持 dev / test / prod（大小写不敏感），"
            "请在 backend/.env 中显式设置后重启；本错误不回显输入内容"
        )


def validate_production_settings(s: Any) -> tuple[list[str], list[str]]:
    """prod 模式安全校验。返回 (violations, warnings)，每条为面向修复动作的说明。"""
    violations: list[str] = []
    warnings: list[str] = []

    # 1. DEBUG：settings 默认 True（settings.py），生产漏配即暴露调试页
    if getattr(s, "DEBUG", False):
        violations.append("DEBUG 处于开启状态：生产环境必须显式设置 DJANGO_DEBUG=false")

    # 2. SECRET_KEY：缺失 / 空白 / 仍为已知开发默认值
    secret_key = getattr(s, "SECRET_KEY", "") or ""
    if not secret_key.strip():
        violations.append("DJANGO_SECRET_KEY 缺失或为空白：必须配置独立强随机密钥（≥32 字节）")
    elif secret_key == DEV_DEFAULT_SECRET_KEY:
        violations.append("DJANGO_SECRET_KEY 仍为开发默认值：必须更换为独立强随机密钥")

    # 3. JWT_SECRET：缺失 / 空白 / 仅依赖对 SECRET_KEY 的默认回退（settings.py 回退逻辑）
    jwt_secret = getattr(s, "JWT_SECRET", "") or ""
    if not jwt_secret.strip():
        violations.append("JWT_SECRET 缺失或为空白：必须显式配置，不得依赖默认回退")
    elif secret_key and jwt_secret == secret_key:
        violations.append("JWT_SECRET 与 DJANGO_SECRET_KEY 相同（回退生效）：必须单独显式配置")

    # 4. ALLOWED_HOSTS：空列表或包含通配符
    allowed_hosts = list(getattr(s, "ALLOWED_HOSTS", []) or [])
    if not allowed_hosts:
        violations.append("ALLOWED_HOSTS 为空：必须配置实际服务域名列表")
    elif "*" in allowed_hosts:
        violations.append("ALLOWED_HOSTS 包含通配符 '*'：必须替换为实际服务域名列表")

    # 5. 数据库：生产禁止回退 SQLite（settings.py 默认 sqlite 文件）
    engine = getattr(s, "DATABASES", {}).get("default", {}).get("ENGINE", "")
    if "sqlite" in engine:
        violations.append("数据库仍为 SQLite 回退配置：生产环境必须显式设置 DATABASE_URL 指向 MySQL")

    # 6. ENROLLMENT_MOCK_CLIENTS：生产禁止 mock 客户端替身进入报名资格校验链
    #    （2026-10-28 加固）：人员审核状态/禁用状态/必需附件等终审校验不得被
    #    mock 派生数据绕过。缺失、开启、非法取值一律拒绝启动（安全失败），
    #    仅显式 False 放行；运行期另有 enrollments.clients.mock_enabled 的
    #    prod 强制关闭双保险。
    mock_flag = getattr(s, "ENROLLMENT_MOCK_CLIENTS", None)
    if mock_flag is not False:
        detail = (
            "未配置"
            if mock_flag is None
            else f"配置非法（{mock_flag!r}）" if not isinstance(mock_flag, bool)
            else "处于开启状态"
        )
        violations.append(
            f"ENROLLMENT_MOCK_CLIENTS {detail}：生产环境必须显式设置 "
            "ENROLLMENT_MOCK_CLIENTS=false，mock 客户端替身不得作为报名资格"
            "校验（人员审核状态/禁用状态/必需附件）的替代"
        )

    # ---- 以下为告警项（不阻止启动，但不得视为已通过检查）----

    # CRYPTO_KEY：临时告警。人员敏感字段加解密上线前必须升级为 violation（见模块 docstring）
    if not (getattr(s, "CRYPTO_KEY", "") or "").strip():
        warnings.append(
            "CRYPTO_KEY 未配置（当前回退为 JWT_SECRET 派生，仅限开发便利）："
            "人员敏感字段加密功能上线前必须配置独立密钥并升级为强制拒绝项"
        )

    # CORS_ORIGINS：沿用开发默认来源即告警（生产前端来源应显式配置）
    cors = {item.strip() for item in getattr(s, "CORS_ORIGINS", []) or []}
    if cors and cors <= _DEV_DEFAULT_CORS:
        warnings.append("CORS_ORIGINS 仍为本地开发默认来源：生产环境必须改为实际前端域名")

    # REDIS_URL：空时缓存回退 LocMemCache——令牌撤销与限流进程内不共享；
    # 单实例可用，多实例部署必须配置（部署验收项，启动时无法判定实例数）
    if not (getattr(s, "REDIS_URL", "") or "").strip():
        warnings.append(
            "REDIS_URL 未配置：缓存为进程内 LocMemCache，重启即丢失令牌撤销与限流窗口，"
            "多实例部署必须配置共享 Redis"
        )

    # 时区：业务契约要求 Asia/Shanghai（报名截止等按上海时间），偏离即告警
    if (getattr(s, "TIME_ZONE", "") or "") != "Asia/Shanghai":
        warnings.append("TIME_ZONE 非 Asia/Shanghai：报名截止等业务时间语义将偏离赛程约定")

    return violations, warnings
