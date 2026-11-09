"""角色、权限点注册表与校验入口。

契约 3：每个非公开操作同时校验 角色 → 权限点 api:{operationId} → 数据范围
→ 对象归属/状态。本模块负责前两层，数据范围见 scopes.py。

约定：新增接口必须在自己的 api.py 导入期调用 register_operation("xxx", Roles...)；
视图首行调用 require("xxx") 完成校验并取回当前账号。漏登记会直接报错，
避免新接口绕过权限。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from django.core.exceptions import ImproperlyConfigured

from .context import get_current_account
from .errors import FORBIDDEN, UNAUTHENTICATED, ApiError

# ---------------------------------------------------------------- 角色

TEAM = "TEAM"
UNIVERSITY = "UNIVERSITY"
CITY = "CITY"
PROVINCE_REVIEWER = "PROVINCE_REVIEWER"
ATHLETE = "ATHLETE"
COACH = "COACH"
ADMIN = "ADMIN"
MATCH_RECORDER = "MATCH_RECORDER"

ALL_ROLES: tuple[str, ...] = (
    TEAM,
    UNIVERSITY,
    CITY,
    PROVINCE_REVIEWER,
    ATHLETE,
    COACH,
    ADMIN,
    MATCH_RECORDER,
)

ROLE_NAMES: dict[str, str] = {
    TEAM: "中小学球队端",
    UNIVERSITY: "学校端",
    CITY: "市州端",
    PROVINCE_REVIEWER: "省级审核",
    ATHLETE: "运动员",
    COACH: "教练员",
    ADMIN: "管理员",
    MATCH_RECORDER: "赛事录入员",
}

# 高权限角色：只有本身具备 ADMIN 的账号才能授予或改动同等级账号
HIGH_PRIVILEGE_ROLES: frozenset[str] = frozenset({ADMIN})

# 具备组织树数据范围的角色（其余角色只限本人/本队）
ORG_SCOPE_ROLES: frozenset[str] = frozenset({ADMIN, PROVINCE_REVIEWER, CITY})

# 邀请凭证只允许 ATHLETE / COACH 业务身份
INVITE_KINDS: tuple[str, ...] = (ATHLETE, COACH)

# 需要 Idempotency-Key 的操作（契约 2.2）
IDEMPOTENT_OPERATIONS: frozenset[str] = frozenset(
    {"identityCreate", "submitEnrollment", "matchEventCreate", "selectionGenerate"}
)

# ---------------------------------------------------------------- 菜单

# 菜单键、路径与角色必须与前端 zuqiu/src/config/menus.ts 保持一致：
# 前端目前用本地 config 渲染侧边栏，不消费 /me.menus，但第三方客户端会读取
# 该字段，两套数据不一致会让外部端拿到错误入口。改任一菜单时同步本表。
# 路径统一为控制台 /console/*；permission 取该页面的代表性权限点。
MENUS: list[dict] = [
    # 总览
    {
        "key": "dashboard",
        "title": "工作台",
        "path": "/console/dashboard",
        "permission": "api:currentUser",
        "roles": list(ALL_ROLES),
    },
    # 报名管理
    {
        "key": "enrollments",
        "title": "报名单",
        "path": "/console/enrollments",
        "permission": "api:enrollmentList",
        # 与路由白名单一致：市州初审、省级终审、管理员也需查看报名清单。
        "roles": [TEAM, UNIVERSITY, CITY, PROVINCE_REVIEWER, ADMIN],
    },
    {
        "key": "enrollment-stats",
        "title": "报名统计",
        "path": "/console/enrollment-stats",
        "permission": "api:enrollmentStats",
        "roles": [CITY, PROVINCE_REVIEWER, ADMIN],
    },
    {
        "key": "review",
        "title": "审核工作台",
        "path": "/console/review",
        "permission": "api:reviewEnrollment",
        "roles": [CITY, PROVINCE_REVIEWER, ADMIN],
    },
    # 赛事与比赛
    {
        "key": "matches",
        "title": "比赛管理",
        "path": "/console/matches",
        "permission": "api:matchList",
        "roles": [ADMIN, PROVINCE_REVIEWER, MATCH_RECORDER, TEAM, UNIVERSITY, CITY],
    },
    {
        "key": "events",
        "title": "赛事与组别",
        "path": "/console/events",
        "permission": "api:eventList",
        "roles": [ADMIN, PROVINCE_REVIEWER, CITY],
    },
    {
        "key": "tournament-stats",
        "title": "赛事统计",
        "path": "/console/tournament-stats",
        "permission": "api:standingList",
        "roles": [PROVINCE_REVIEWER, ADMIN],
    },
    # 赛务核验
    {
        "key": "identity",
        "title": "身份核验",
        "path": "/console/identity",
        "permission": "api:identityList",
        "roles": [ADMIN, PROVINCE_REVIEWER, MATCH_RECORDER],
    },
    # 学校管理
    {
        "key": "school-teams",
        "title": "本校球队",
        "path": "/console/school/teams",
        "permission": "api:teamList",
        "roles": [UNIVERSITY],
    },
    # 球队管理
    {
        "key": "teams-overview",
        "title": "参赛队伍",
        "path": "/console/teams-overview",
        "permission": "api:teamList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "team-profile",
        "title": "球队资料",
        "path": "/console/team/profile",
        "permission": "api:teamGet",
        "roles": [TEAM, UNIVERSITY],
    },
    {
        "key": "team-members",
        "title": "球员名单",
        "path": "/console/team/members",
        "permission": "api:memberList",
        "roles": [TEAM, UNIVERSITY],
    },
    {
        "key": "team-coaches",
        "title": "教练团队",
        "path": "/console/team/coaches",
        "permission": "api:teamCoachList",
        "roles": [TEAM, UNIVERSITY],
    },
    # 个人中心
    {
        "key": "profile",
        "title": "个人资料",
        "path": "/console/profile",
        "permission": "api:myProfileGet",
        "roles": [ATHLETE, COACH],
    },
    {
        "key": "materials",
        "title": "履历材料",
        "path": "/console/materials",
        "permission": "api:materialList",
        "roles": [ATHLETE, COACH],
    },
    {
        "key": "my-teams",
        "title": "我的球队",
        "path": "/console/my-teams",
        "permission": "api:myTeams",
        "roles": [ATHLETE, COACH],
    },
    {
        "key": "my-publications",
        "title": "我的公示",
        "path": "/console/my-publications",
        "permission": "api:selectionPublicityMine",
        "roles": [ATHLETE, COACH],
    },
    {
        "key": "my-competition",
        "title": "我的赛事数据",
        "path": "/console/my-competition",
        "permission": "api:myCompetitions",
        "roles": [ATHLETE, COACH],
    },
    # 遴选管理
    {
        "key": "dossiers",
        "title": "球员遴选档案",
        "path": "/console/selection/dossiers",
        "permission": "api:dossierSearch",
        "roles": [ADMIN],
    },
    {
        "key": "rules",
        "title": "评分模板",
        "path": "/console/selection/rules",
        "permission": "api:selectionRuleList",
        "roles": [ADMIN],
    },
    {
        "key": "scoring",
        "title": "评分录入",
        "path": "/console/selection/scoring",
        "permission": "api:selectionScoreList",
        "roles": [ADMIN],
    },
    {
        "key": "summary",
        "title": "评分汇总",
        "path": "/console/selection/summary",
        "permission": "api:selectionSummary",
        "roles": [ADMIN],
    },
    {
        "key": "results",
        "title": "最佳球员遴选结果",
        "path": "/console/selection/results",
        "permission": "api:selectionResults",
        "roles": [ADMIN],
    },
    # 门户运营
    {
        "key": "contents",
        "title": "内容管理",
        "path": "/console/cms/contents",
        "permission": "api:contentList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "resources",
        "title": "门户资源",
        "path": "/console/cms/resources",
        "permission": "api:portalResourceList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "publications",
        "title": "公示管理",
        "path": "/console/publications",
        "permission": "api:publicationList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    # 系统管理
    {
        "key": "people",
        "title": "人员管理",
        "path": "/console/admin/people",
        "permission": "api:personList",
        # 学校端建档/维护/提交，市州与省级审核（2026-11-09 职责分离）。
        "roles": [ADMIN, UNIVERSITY, CITY, PROVINCE_REVIEWER],
    },
    {
        "key": "subjects",
        "title": "参赛主体",
        "path": "/console/admin/subjects",
        "permission": "api:organizationList",
        "roles": [ADMIN],
    },
    {
        "key": "accounts",
        "title": "参赛账号",
        "path": "/console/admin/accounts",
        "permission": "api:accountList",
        "roles": [ADMIN, PROVINCE_REVIEWER, CITY, UNIVERSITY],
    },
    {
        "key": "permissions",
        "title": "权限矩阵",
        "path": "/console/admin/permissions",
        "permission": "api:accountList",
        "roles": [ADMIN],
    },
    {
        "key": "organizations",
        "title": "组织结构",
        "path": "/console/admin/organizations",
        "permission": "api:organizationList",
        "roles": [ADMIN],
    },
    {
        "key": "dictionaries",
        "title": "数据字典",
        "path": "/console/admin/dictionaries",
        "permission": "api:dictionaryList",
        "roles": [ADMIN],
    },
    {
        "key": "logs",
        "title": "操作日志",
        "path": "/console/admin/logs",
        "permission": "api:auditList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    # 消息中心
    {
        "key": "messages",
        "title": "我的消息",
        "path": "/console/messages",
        "permission": "api:messageList",
        "roles": list(ALL_ROLES),
    },
]

# ---------------------------------------------------------------- 权限点

OPERATION_ROLES: dict[str, tuple[str, ...]] = {}


def register_operation(operation_id: str, roles: Sequence[str]) -> tuple[str, ...]:
    """登记权限点；重复登记以最新为准，角色必须合法。"""
    invalid = [role for role in roles if role not in ALL_ROLES]
    if invalid:
        raise ImproperlyConfigured(
            f"权限点 {operation_id} 含未知角色：{invalid}"
        )
    resolved = tuple(dict.fromkeys(roles))
    OPERATION_ROLES[operation_id] = resolved
    return resolved


def operation_roles(operation_id: str) -> tuple[str, ...]:
    return OPERATION_ROLES.get(operation_id, ())


# 成员2 负责的账号/组织/字典/审计权限点统一在 apps/accounts/api.py 导入期登记，
# 避免 permissions.py 与 api.py 两处注册互相覆盖（改一处不生效）。
register_operation("messageList", ALL_ROLES)
register_operation("messageGet", ALL_ROLES)
register_operation("messageRead", ALL_ROLES)

register_operation("fileUpload", ALL_ROLES)
register_operation("fileBatchUpload", ALL_ROLES)
register_operation("fileGet", ALL_ROLES)
register_operation("fileDownload", ALL_ROLES)
register_operation("fileDelete", ALL_ROLES)

register_operation("identityCreate", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityList", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityGet", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityReview", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))


def permissions_for(roles: Optional[Iterable[str]]) -> list[str]:
    """当前角色可用的权限点集合，供 /me 返回。"""
    role_set = set(roles or ())
    return sorted(
        f"api:{operation}"
        for operation, allowed in OPERATION_ROLES.items()
        if role_set & set(allowed)
    )


def menus_for(roles: Optional[Iterable[str]]) -> list[dict]:
    """按角色过滤菜单；对外不暴露 roles 字段。"""
    role_set = set(roles or ())

    def build(items: list[dict]) -> list[dict]:
        result: list[dict] = []
        for item in items:
            if not role_set & set(item.get("roles", ())):
                continue
            node = {key: value for key, value in item.items() if key != "roles"}
            children = build(item.get("children") or [])
            if children:
                node["children"] = children
            result.append(node)
        return result

    return build(MENUS)


# ---------------------------------------------------------------- 校验入口


def require(operation_id: str) -> Optional[object]:
    """视图首行调用：校验登录态 + 角色 + 权限点，返回当前账号。

    未登记的 operationId 视为开发错误，直接抛配置异常（不是 403），
    以免新接口悄悄绕过权限。
    """
    allowed = OPERATION_ROLES.get(operation_id)
    if allowed is None:
        raise ImproperlyConfigured(
            f"接口 {operation_id} 未登记权限点，请在 api.py 导入期 register_operation()"
        )

    account = get_current_account()
    if account is None:
        raise ApiError(UNAUTHENTICATED)
    if not getattr(account, "enabled", False):
        raise ApiError(FORBIDDEN, "账号已被停用")

    if not set(getattr(account, "roles", ()) or ()) & set(allowed):
        raise ApiError(
            FORBIDDEN,
            f"当前角色无权执行 {operation_id}",
            [
                {
                    "field": "role",
                    "reason": "需要以下角色之一：" + ",".join(allowed),
                }
            ],
        )
    return account


def current_account() -> Optional[object]:
    return get_current_account()


# ---------------------------------------------------------------- 防自审（P1-5）


def assert_not_self_review(
    account,
    object_type: str,
    object_id: str,
    actions: tuple[str, ...],
    *,
    allow_missing_evidence: bool = False,
) -> None:
    """防自审校验：当前账号不得审核自己创建或修改的对象。

    业务依据（2026-10-30 P1-5 裁定）：
    - 同一账号创建或修改过的人员档案、材料，不得由该账号审核；
      账号拥有多个角色（如 ADMIN+UNIVERSITY）也不改变该规则——
      匹配键为 operator_account_id（账号主键），与角色无关。
    - person.self_confirm 不作为创建或修改证据（仅改 audit_status）。
    - material.remove 不纳入（软删后对象不可审核）。
    - 无法找到对应的成功创建或修改 AuditLog 时，默认保持阻断，不自动放行。

    判定逻辑（查询 AuditLog.accounts_audit_log）：
    - 无任何成功创建/修改记录 → 阻断（缺乏审计证据，无法排除自审）；
    - 存在当前账号的创建/修改记录 → 阻断（自审）；
    - 仅存在其他账号的记录 → 放行。

    例外（2026-11-02 管理端联调）：`allow_missing_evidence=True` 时，
    仅「无任何创建/修改记录」分支放行，「当前账号创建/修改过」仍阻断。
    调用方仅限材料审核中的全局 ADMIN：管理端人员管理/参赛队伍页面按接口
    契约（docs §materialReview 角色含 ADMIN）提供「通过/驳回」按钮，而演练
    与演示数据由 seed 脚本直写数据库、不产生 API 审计日志，按缺证据阻断会
    使该功能对全量种子材料恒 403；ADMIN 本身是全局数据范围，不存在越权
    扩面，自审风险仍由第二分支覆盖。学校端（UNIVERSITY）不启用该例外。

    阻断仅表示缺乏足够审计证据或存在自审嫌疑，错误信息不宣称已证明发生自审。
    依赖 AuditLog 由 AuditMiddleware 在响应后写入；业务提交与审计写入非同一
    事务的缺口为已知风险（本轮不改造 AuditMiddleware）。
    """
    from apps.accounts.models import AuditLog  # 延迟导入：accounts.models 不依赖本模块

    operators = set(
        AuditLog.objects.filter(
            object_type=object_type,
            object_id=object_id,
            action__in=actions,
            outcome="SUCCESS",
        ).values_list("operator_account_id", flat=True)
    )
    if not operators:
        if allow_missing_evidence:
            return
        raise ApiError(FORBIDDEN, "当前账号无权审核该记录")
    if str(account.pk) in operators:
        raise ApiError(FORBIDDEN, "当前账号无权审核该记录")
