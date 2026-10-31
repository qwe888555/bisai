"""材料域业务逻辑：材料列表 / 创建 / 审核 / 软删（成员4 域，批次 C）。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）+ zuqiu 前端实码契约。
- 多态：material_kind(PERSON/TEAM) 分派 PersonMaterial / TeamMaterial；
  审核历史 MaterialAuditLog、文件版本 MaterialFileVersion 为多态逻辑引用（无 FK）；
  review/remove 按 id 双表解析：两表同时命中（同 id 有效行）→ 409 归属不明确
  不静默择一（L11 待前端补传 ownerType）；
- 材料类型以 material_type 表为唯一事实源（不硬编码 13 种类型、不编造字典项）：
  类型表为空/无效/停用/归属不匹配 → 创建 400 明确报错，不伪造成功
  （2026-10-29 问题⑥裁定，原 409；409 保留给真实资源状态冲突）；
  列表不受类型表影响（空表返回空结果属正常）；
- 状态机：创建即 PENDING（need_audit=False 类型免审直通 APPROVED；前端创建文案
  "材料已提交审核"）；审核仅 PENDING → APPROVED/REJECTED（REJECT 必填 reason）；
  DRAFT/WITHDRAWN 本期不产生（前端无草稿/撤回调用）；移除为软删（is_deleted），
  任意状态可软删（前端 removeMaterial 无状态限制）；
- 文件绑定（复用 attachments 文件服务）：file_id 必须为已存在 FileRecord 且
  owner_type 与材料归属一致；owner_id 为空（先传后绑，前端材料页上传时
  ownerId 可为空串）则创建后回填归属（同 persons._bind_avatar 先例），
  已绑定但 owner_id 与材料归属不一致 → 409（不得接受任意文件 ID）；
  回填为仅当 owner_id 仍为空的原子条件更新，未取得绑定权（并发被抢绑）→
  409 整单回滚，防并发双绑；
- R6：type.allow_multiple=False 时同归属+类型 有效（未软删）材料唯一，服务层强制；
- version 乐观锁按 B4 规则由 updated_at 微秒派生（复用 persons.ensure_version）；
  写链禁 queryset.update() 绕过 auto_now 影响版本派生（文件归属回填除外——
  FileRecord 非 version 事实源）；
- 数据范围（2026-10-28 统一业务规则+一·补四+D1/D5 批准）：全局 ADMIN 全库
  （D1：仅数据范围能力，普通审核权限已移除，见 materialReview 登记注释）；
  UNIVERSITY 本校（School.code ↔ Organization.code 编码映射）；TEAM 仅本队
  TEAM 材料（绑定依据 accounts.Account.team_ids，ADMIN 账号管理维护，
  attachments 域已作权威归属校验先例；PERSON 材料对球队端不可见）；无有效
  范围（无绑定/空绑定）默认拒绝，不退化；其余角色保持拒绝；
  归属对象无学校归属时对受限账号 deny-by-default；
- 列表筛选与审核历史（P1-9 原定任务，2026-10-28 统一业务规则批准补齐）：
  status/needAudit/keyword（keyword 按材料标题 remark 包含匹配）；审核历史
  GET /materials/{id}/audit-logs 多态解析与数据范围同 material_get。
"""

from __future__ import annotations

from itertools import chain
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attachments.models import FileRecord
from apps.common import enums
from apps.common.errors import (
    DATA_SCOPE_DENIED,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
)
from apps.common.permissions import ADMIN, TEAM, UNIVERSITY, assert_not_self_review
from apps.common.scopes import scope_org_codes
from apps.materials.models import (
    MaterialAuditLog,
    MaterialFileVersion,
    MaterialType,
    PersonMaterial,
    TeamMaterial,
)
from apps.persons.models import Person
from apps.teams.models import Team


def _material_scope(account) -> tuple:
    """材料数据范围（2026-10-28 D1/D5 批准）：返回 (school_codes, team_ids)。

    - 全局 ADMIN（无组织绑定）→ (None, None) 全库。D1：数据范围能力保留
      （系统管理/异常处理），普通材料审核权限已从 materialReview 移除，
      不因范围推导审核权；材料域挂组织 ADMIN 维持全库（对齐子树三态列为
      B 类建议，未获批准不实施）；
    - UNIVERSITY → (codes, None)：所属组织子树编码（本校，A′ 编码映射）；
      无有效组织范围（无绑定/绑定已软删）→ 默认拒绝，不退化为全库；
    - TEAM → (None, ids)：仅本队 TEAM 材料（一·补四：球队端提交/维护/补充
      本队材料；审核权限不开放）。绑定依据 accounts.Account.team_ids
      （ADMIN 账号管理写入，attachments 域已作权威归属校验先例），集合
      语义校验服务端归属、不信任客户端传参；空绑定 → 拒绝；PERSON 材料
      对球队端一律不可见；
    - 其余角色（CITY/PROVINCE_REVIEWER/ATHLETE/COACH）保持拒绝。
    """
    roles = getattr(account, "roles", ()) or ()
    if ADMIN in roles:
        return None, None
    if UNIVERSITY in roles:
        codes = scope_org_codes(account)
        if not codes:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "当前账号无有效组织范围，禁止访问材料数据",
            )
        return codes, None
    if TEAM in roles:
        raw = getattr(account, "team_ids", None) or []
        ids = sorted({int(t) for t in raw if str(t).lstrip("-").isdigit()})
        if not ids:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "当前球队账号未绑定任何球队，禁止访问材料数据",
            )
        return None, ids
    raise ApiError(
        DATA_SCOPE_DENIED,
        "材料数据范围仅对管理员、学校与球队账号开放",
    )


def _ensure_owner_in_scope(owner, scope: tuple) -> None:
    """材料归属对象（Person/Team）必须在账号范围内（单对象/写链共用）。

    scope=(None, None)（全局 ADMIN）直接放行；TEAM 范围：归属必须是 Team
    且主键在本队集合内（PERSON 材料对球队端不可见）；学校范围 deny-by-
    default：归属对象无学校归属（Person/Team.school 可空）即无法证明在
    授权范围内 → 403（同 A′ 写链显式拒绝口径，不静默放行）。
    """
    codes, team_ids = scope
    if codes is None and team_ids is None:
        return
    if team_ids is not None:
        if not isinstance(owner, Team) or owner.pk not in set(team_ids):
            raise ApiError(
                DATA_SCOPE_DENIED,
                "目标材料不在当前球队账号的本队范围内",
            )
        return
    school = getattr(owner, "school", None)
    if school is None or school.code not in set(codes):
        raise ApiError(
            DATA_SCOPE_DENIED,
            "目标材料归属学校不在当前账号数据范围内",
        )


def _apply_scope(queryset, kind: str, scope: tuple):
    """列表查询按范围过滤：学校编码（codes）或本队集合（team_ids）。"""
    codes, team_ids = scope
    if codes is None and team_ids is None:
        return queryset
    if team_ids is not None:
        if kind != "TEAM":  # 人员材料对球队端不可见（一·补四）
            return queryset.none()
        return queryset.filter(team_id__in=team_ids)
    field = "person__school__code" if kind == "PERSON" else "team__school__code"
    return queryset.filter(**{f"{field}__in": codes})


def _parse_list_filters(filters: dict) -> tuple:
    """P1-9 列表筛选参数解析：status/needAudit/keyword。

    status 取 MaterialStatus 全枚举（无效 400）；needAudit 仅 true/false
    （映射到 MaterialType.need_audit）；keyword 去首尾空白后按材料标题
    remark 包含匹配（口径：标题即 remark，contract title 无独立列）。
    返回 (status, need_audit, keyword)。
    """
    status = filters.get("status")
    if status not in (None, ""):
        status = str(status).strip().upper()
        if status not in enums.MaterialStatus.values:
            raise ApiError(
                INVALID_ARGUMENT,
                "status 仅支持 DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN",
                [{"field": "status", "reason": "不在枚举范围内"}],
            )
    else:
        status = None
    need_audit = filters.get("needAudit")
    if need_audit not in (None, ""):
        raw = str(need_audit).strip().lower()
        if raw not in ("true", "false"):
            raise ApiError(
                INVALID_ARGUMENT,
                "needAudit 仅支持 true/false",
                [{"field": "needAudit", "reason": "布尔值只能是 true/false"}],
            )
        need_audit = raw == "true"
    else:
        need_audit = None
    keyword = str(filters.get("keyword") or "").strip()
    return status, need_audit, keyword


def _apply_list_filters(queryset, status, need_audit, keyword: str):
    """列表筛选落查询（均通过解析层校验后才会到达）。"""
    if status:
        queryset = queryset.filter(status=status)
    if need_audit is not None:
        queryset = queryset.filter(type__need_audit=need_audit)
    if keyword:
        queryset = queryset.filter(remark__icontains=keyword)
    return queryset


def _resolve_owner(owner_type: str, owner_id: str):
    """归属对象存在且未软删（PERSON→Person / TEAM→Team），否则 404。"""
    from apps.persons.services import parse_path_id

    if owner_type == "PERSON":
        owner = Person.objects.filter(
            pk=parse_path_id(owner_id, "ownerId"), is_deleted=False
        ).first()
        if owner is None:
            raise ApiError(NOT_FOUND, "材料归属人员不存在或已删除")
        return owner
    owner = Team.objects.filter(
        pk=parse_path_id(owner_id, "ownerId"), is_deleted=False
    ).first()
    if owner is None:
        raise ApiError(NOT_FOUND, "材料归属球队不存在或已删除")
    return owner


def _resolve_material_type(code: str, owner_type: str) -> MaterialType:
    """类型必须存在于 material_type 表且启用且归属匹配（空表 → 400 明确报错）。

    2026-10-29 问题⑥裁定：类型不存在/停用/归属不匹配属于请求引用了
    无效的类型字典项（配置/参数错误），语义为 400 INVALID_ARGUMENT，
    不再使用 409 STATE_CONFLICT（409 保留给真实资源状态冲突）。
    """
    mtype = MaterialType.objects.filter(code=code).first()
    if mtype is None:
        raise ApiError(
            INVALID_ARGUMENT,
            f"材料类型 {code} 不存在：类型字典尚未初始化，暂无法受理材料创建",
        )
    if not mtype.is_active:
        raise ApiError(INVALID_ARGUMENT, f"材料类型 {code} 已停用")
    if mtype.applies_to != owner_type:
        raise ApiError(INVALID_ARGUMENT, f"材料类型 {code} 不适用于 {owner_type} 归属")
    return mtype


def _resolve_file(file_id: str, owner_type: str, owner_id: int) -> FileRecord:
    """文件必须存在、归属类型一致；未绑定（先传后绑）或已绑定本人/本队方可使用。"""
    record = FileRecord.objects.filter(pk=str(file_id).strip()).first()
    if record is None:
        raise ApiError(NOT_FOUND, "材料文件不存在或已删除")
    if record.owner_type != owner_type:
        raise ApiError(
            STATE_CONFLICT,
            f"文件归属类型为 {record.owner_type}，与材料归属 {owner_type} 不一致",
        )
    if record.owner_id and record.owner_id != str(owner_id):
        raise ApiError(STATE_CONFLICT, "文件已绑定其他归属对象，不能重复使用")
    return record


def _r6_duplicate(mtype: MaterialType, owner_type: str, owner_id: int) -> bool:
    """R6：allow_multiple=False 时同归属+类型存在有效（未软删）材料。"""
    if mtype.allow_multiple:
        return False
    model = PersonMaterial if owner_type == "PERSON" else TeamMaterial
    owner_field = "person_id" if owner_type == "PERSON" else "team_id"
    return model.objects.filter(
        **{owner_field: owner_id}, type=mtype, is_deleted=False
    ).exists()


def _material_model(kind: str):
    return PersonMaterial if kind == "PERSON" else TeamMaterial


def _active_queryset(kind: str):
    model = _material_model(kind)
    return model.objects.filter(is_deleted=False)


def _extension(name: str) -> Optional[str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext[:10] or None


def _parse_owner_id(raw) -> int:
    from apps.persons.services import parse_path_id

    return parse_path_id(raw, "ownerId")


# ---------------------------------------------------------------- 列表

def material_list(filters: dict, account) -> list:
    """材料列表（软删排除；ownerType/ownerId 可选，缺省为全局跨两表列表）。

    无 ownerType 时（Dashboard 统计）合并两张同构材料表按创建时间倒序；
    数据量增长后应改为归一化视图/联合查询（联调注记）。
    返回 (items, kind) 元组列表，供序列化层携带多态 kind。
    """
    scope = _material_scope(account)
    status, need_audit, keyword = _parse_list_filters(filters)
    owner_type = filters.get("ownerType")
    owner_id = filters.get("ownerId")

    if owner_type not in (None, "", "PERSON", "TEAM"):
        raise ApiError(
            INVALID_ARGUMENT,
            "ownerType 仅支持 PERSON/TEAM",
            [{"field": "ownerType", "reason": "不在枚举范围内"}],
        )
    if owner_type in ("PERSON", "TEAM"):
        if not owner_id:
            raise ApiError(
                INVALID_ARGUMENT,
                "指定 ownerType 时必须同时提供 ownerId",
                [{"field": "ownerId", "reason": "必填"}],
            )
        owner_pk = _parse_owner_id(owner_id)
        kind = owner_type
        rows = (
            _apply_scope(_active_queryset(kind), kind, scope)
            .select_related("person" if kind == "PERSON" else "team", "type")
            .filter(**({"person_id": owner_pk} if kind == "PERSON" else {"team_id": owner_pk}))
            .order_by("-created_at", "-id")
        )
        rows = _apply_list_filters(rows, status, need_audit, keyword)
        return [(m, kind) for m in rows]
    person_rows = _apply_list_filters(
        _apply_scope(_active_queryset("PERSON"), "PERSON", scope),
        status, need_audit, keyword,
    ).select_related("person", "type")
    team_rows = _apply_list_filters(
        _apply_scope(_active_queryset("TEAM"), "TEAM", scope),
        status, need_audit, keyword,
    ).select_related("team", "type")
    merged = sorted(
        chain(person_rows, team_rows),
        key=lambda m: (m.created_at, m.pk),
        reverse=True,
    )
    return [(m, "PERSON" if isinstance(m, PersonMaterial) else "TEAM") for m in merged]


# ---------------------------------------------------------------- 阶段 1 批量查询

def _validate_batch_query_args(owner_type, owner_ids, type_codes) -> tuple:
    """批量查询入参校验（步骤1-4），返回 (parsed_ids, valid_types)。

    供 material_batch_query（账号级）与 material_batch_query_internal
    （系统级）共用；校验契约见 material_batch_query docstring。
    """
    # 1. owner_type 校验
    if owner_type not in ("PERSON", "TEAM"):
        raise ApiError(
            INVALID_ARGUMENT,
            "ownerType 仅支持 PERSON/TEAM",
            [{"field": "ownerType", "reason": "不在枚举范围内"}],
        )

    # 2. owner_ids 校验：接受 list/tuple，正整数，拒绝 bool/浮点数/数字字符串
    if not isinstance(owner_ids, (list, tuple)):
        raise ApiError(INVALID_ARGUMENT, "ownerIds 必须为列表或元组")
    parsed_ids = []
    seen_ids = set()
    for raw in owner_ids:
        if isinstance(raw, bool) or isinstance(raw, float) or isinstance(raw, str):
            raise ApiError(INVALID_ARGUMENT, "ownerIds 元素必须为正整数")
        if not isinstance(raw, int) or raw <= 0:
            raise ApiError(INVALID_ARGUMENT, "ownerIds 元素必须为正整数")
        if raw not in seen_ids:
            seen_ids.add(raw)
            parsed_ids.append(raw)
    if not parsed_ids:
        raise ApiError(INVALID_ARGUMENT, "ownerIds 不能为空")

    # 3. type_codes 校验：strip、空串丢弃、去重保序
    if not isinstance(type_codes, (list, tuple)):
        raise ApiError(INVALID_ARGUMENT, "typeCodes 必须为列表或元组")
    seen_codes = set()
    normalized_codes = []  # [(原始输入值, 规范化后码)]
    for orig in type_codes:
        stripped = str(orig).strip()
        if not stripped:
            continue
        if stripped in seen_codes:
            continue
        seen_codes.add(stripped)
        normalized_codes.append((orig, stripped))
    if not normalized_codes:
        raise ApiError(INVALID_ARGUMENT, "typeCodes 不能为空")

    # 4. 批量类型预检：存在性 + applies_to 匹配（不检查 is_active，历史材料可查）
    problems = []
    valid_types = {}  # code -> MaterialType
    for orig, code in normalized_codes:
        mtype = MaterialType.objects.filter(code=code).first()
        if mtype is None:
            problems.append({
                "field": "type_codes",
                "value": orig,
                "reason": f"材料类型 {code} 不存在",
            })
            continue
        if mtype.applies_to != owner_type:
            problems.append({
                "field": "type_codes",
                "value": orig,
                "reason": f"材料类型 {code} 不适用于 {owner_type} 归属",
            })
            continue
        valid_types[code] = mtype
    if problems:
        raise ApiError(INVALID_ARGUMENT, "材料类型校验失败", problems)
    return parsed_ids, valid_types


def _batch_query_materials(parsed_ids: list, valid_types: dict, owner_type: str, scope=None) -> dict:
    """批量查询主体（步骤6-9），返回 items + missing_owner_ids。

    scope 传 None 时为系统级查询（不叠加数据范围）；账号级入口传
    _material_scope(account) 结果以保持原行为。
    """
    # 6. 查询材料（不增加 type__is_active 过滤；is_active=False 历史材料可查）
    MaterialModel = PersonMaterial if owner_type == "PERSON" else TeamMaterial
    owner_field = "person_id" if owner_type == "PERSON" else "team_id"
    valid_codes = list(valid_types.keys())
    materials_qs = MaterialModel.objects.filter(
        is_deleted=False,
        **{f"{owner_field}__in": parsed_ids},
        type__code__in=valid_codes,
    ).select_related("type")
    if scope is not None:
        materials_qs = _apply_scope(materials_qs, owner_type, scope)

    # 7. 排序：owner_id ASC → type_code ASC → updated_at DESC → material_id DESC
    material_rows = sorted(
        materials_qs,
        key=lambda m: (
            getattr(m, owner_field),
            m.type.code,
            -m.updated_at.timestamp() if m.updated_at else 0,
            -m.pk,
        ),
    )

    # 8. 构建 items（恰好 8 字段）
    items = []
    owner_ids_with_materials = set()
    for m in material_rows:
        oid = getattr(m, owner_field)
        owner_ids_with_materials.add(oid)
        items.append({
            "owner_id": oid,
            "material_id": m.pk,
            "type_code": m.type.code,
            "status": m.status,
            "need_audit": m.type.need_audit,
            "allow_multiple": m.type.allow_multiple,
            "file_id": m.file_id,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        })

    # 9. missing_owner_ids：无有效材料记录的归属 ID，按去重后输入顺序
    missing_owner_ids = [oid for oid in parsed_ids if oid not in owner_ids_with_materials]

    return {"items": items, "missing_owner_ids": missing_owner_ids}


def material_batch_query_internal(owner_type, owner_ids, type_codes) -> dict:
    """阶段 1 系统级批量查询（只读，无账号数据范围）。

    与 material_batch_query 的差异：不做 _material_scope / 归属范围校验，
    供无请求账号的系统级校验使用（报名预检/终审；persons 域
    person_audit_snapshot 同款先例）。入参校验、类型预检、items/排序/
    missing_owner_ids 契约与账号级完全一致。
    """
    parsed_ids, valid_types = _validate_batch_query_args(owner_type, owner_ids, type_codes)
    return _batch_query_materials(parsed_ids, valid_types, owner_type)


def material_batch_query(owner_type, owner_ids, type_codes, account) -> dict:
    """阶段 1：多归属多类型材料批量查询（只读，不替换 material_list）。

    入参契约（见《材料域阶段 1 完整契约清单》）：
    - owner_type: 仅 "PERSON"/"TEAM"（大小写敏感），非法 → 400 INVALID_ARGUMENT
    - owner_ids: list 或 tuple，元素必须为正整数；拒绝 bool/浮点数/数字字符串；
      任一非法元素整体 400 拒绝（不跳过）；去重保序；不增加额外业务上限。
    - type_codes: list 或 tuple；逐元素 strip()，空串丢弃，去重保序；
      规范化后无有效码 → 400 INVALID_ARGUMENT。
    - 批量类型预检：不存在 / applies_to 不匹配 → 400，problems 数组按输入顺序，
      每条 {"field":"type_codes","value":"<原始输入值>","reason":"<原因>"}。
    - 返回 {"items":[...8字段...], "missing_owner_ids":[...]}。
      items 字段：owner_id(int)、material_id、type_code、status、need_audit、
      allow_multiple、file_id、updated_at(ISO8601 isoformat)。
      排序：owner_id ASC → type_code ASC → updated_at DESC → material_id DESC。
      missing_owner_ids：输入归属 ID 中无有效材料记录者，按去重后输入顺序。
    - MaterialType.is_active=False 的历史材料仍可查询（不增加 is_active 过滤）。
    - 数据范围复用 _material_scope / _ensure_owner_in_scope；异常向上传播。
    """
    parsed_ids, valid_types = _validate_batch_query_args(owner_type, owner_ids, type_codes)

    # 5. 数据范围：解析归属对象并校验范围
    scope = _material_scope(account)
    OwnerModel = Person if owner_type == "PERSON" else Team
    owners = OwnerModel.objects.filter(pk__in=parsed_ids, is_deleted=False)
    owner_map = {o.pk: o for o in owners}
    for oid in parsed_ids:
        owner = owner_map.get(oid)
        if owner is not None:
            _ensure_owner_in_scope(owner, scope)

    return _batch_query_materials(parsed_ids, valid_types, owner_type, scope)


# ---------------------------------------------------------------- 创建

def material_create(payload: dict, account) -> tuple:
    """创建材料：归属校验 → 类型校验 → 文件校验 → R6 → 状态落库。

    事务内完成：材料行 + 文件归属回填（先传后绑）+ MaterialFileVersion v1 留痕。
    返回 (material, kind)。
    """
    scope = _material_scope(account)
    owner_type = payload["ownerType"]
    owner = _resolve_owner(owner_type, payload["ownerId"])
    _ensure_owner_in_scope(owner, scope)
    mtype = _resolve_material_type(payload["materialType"], owner_type)
    file_ids = payload.get("fileIds") or []
    if len(file_ids) != 1:
        raise ApiError(
            INVALID_ARGUMENT,
            "fileIds 仅支持 1 个文件（冻结模型为单文件承载）",
            [{"field": "fileIds", "reason": "必须恰好 1 个"}],
        )
    record = _resolve_file(file_ids[0], owner_type, owner.pk)

    if _r6_duplicate(mtype, owner_type, owner.pk):
        raise ApiError(
            STATE_CONFLICT,
            f"该归属已存在「{mtype.name}」材料（类型不允许重复提交）",
        )

    kind = owner_type
    with transaction.atomic():
        material = _material_model(kind).objects.create(
            **({"person": owner} if kind == "PERSON" else {"team": owner}),
            type=mtype,
            file_id=record.pk,
            file_name=payload["fileName"],
            file_size=record.size_bytes or None,
            file_ext=_extension(payload["fileName"]),
            status=(
                enums.MaterialStatus.APPROVED
                if not mtype.need_audit
                else enums.MaterialStatus.PENDING
            ),
            remark=payload["title"],
            # uploaded_by 等"逻辑引用"列恒空：底座账号 ID 映射未定（同 persons
            # created_by/updated_by 先例），不写入字符串账号主键
        )
        if not record.owner_id:  # 先传后绑：回填文件归属（同 persons._bind_avatar 先例）
            # 原子条件更新防并发双绑：前置校验与回填之间文件可能已被并发请求
            # 绑定，不能只信前置查询——仅当 owner_id 仍为空时才取得绑定权；
            # 影响行数=0 即绑定失败 → 409 并回滚整单（材料行/版本留痕一并撤销）
            claimed = FileRecord.objects.filter(pk=record.pk, owner_id="").update(
                owner_id=str(owner.pk)
            )
            if claimed != 1:
                raise ApiError(
                    STATE_CONFLICT,
                    "材料文件归属已被其他操作抢先绑定，未能取得该文件的绑定权，"
                    "本次创建已取消，请刷新后重试",
                )
        MaterialFileVersion.objects.create(
            material_kind=kind,
            material_id=material.pk,
            version=1,
            file_id=record.pk,
            file_name=payload["fileName"],
        )
    return material, kind


# ---------------------------------------------------------------- 审核

def material_review(material_id: str, payload: dict, account) -> tuple:
    """审核：仅 PENDING 可处理；APPROVE/REJECT → APPROVED/REJECTED + 审核历史。

    前端 body 无归属类型字段，多态由 _get_material_any 双表解析。

    P1-5 防自审：对象加锁读取、版本校验、状态校验、自审审计校验、状态变更与
    留痕全部在同一事务内执行，select_for_update 锁定待审核材料行。
    """
    scope = _material_scope(account)
    from apps.persons.services import ensure_version, parse_path_id

    # 多态解析 + 数据范围在事务外完成（不写库）；事务内重新加锁读取，
    # 避免在未加锁的对象上做状态判断。
    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_owner_in_scope(
        material.person if kind == "PERSON" else material.team, scope
    )
    with transaction.atomic():
        # 按归属类型重新加锁读取，保留 select_related 供后续字段访问
        if kind == "PERSON":
            locked = (
                PersonMaterial.objects.select_for_update()
                .select_related("person", "type")
                .filter(pk=material.pk, is_deleted=False)
                .first()
            )
        else:
            locked = (
                TeamMaterial.objects.select_for_update()
                .select_related("team", "type")
                .filter(pk=material.pk, is_deleted=False)
                .first()
            )
        if locked is None:
            raise ApiError(NOT_FOUND, "材料不存在或已删除")
        material = locked
        ensure_version(material, payload["version"])
        if material.status != enums.MaterialStatus.PENDING:
            raise ApiError(
                STATE_CONFLICT,
                f"材料当前状态为 {material.status}，仅待审核（PENDING）材料可处理",
            )
        # 防自审：纳入 material.create / material.update；
        # material.remove 不纳入（软删后材料不可审核）。
        assert_not_self_review(
            account,
            "Material",
            f"{kind}:{material.pk}",
            ("material.create", "material.update"),
        )
        decision = payload["decision"]
        reason = (payload.get("reason") or "").strip()
        if decision == "REJECT" and not reason:
            raise ApiError(
                INVALID_ARGUMENT,
                "驳回材料必须填写审核意见",
                [{"field": "reason", "reason": "REJECT 时必填"}],
            )
        from_status = material.status
        material.status = (
            enums.MaterialStatus.APPROVED
            if decision == "APPROVE"
            else enums.MaterialStatus.REJECTED
        )
        material.audit_remark = reason
        # audited_by 为"逻辑引用"列恒空：底座账号 ID 映射未定（同 persons 先例）
        material.audited_at = timezone.now()
        # 状态变更与审核留痕同一事务：日志落库失败 → 状态/版本整体回滚到审核前，
        # 不留下"已改状态、无留痕"的半完成审核结果
        material.save()
        MaterialAuditLog.objects.create(
            material_kind=kind,
            material_id=material.pk,
            from_status=from_status,
            to_status=material.status,
            # auditor 恒空：底座账号 ID（acc_* 字符串）→ BigInteger 逻辑引用
            # 映射未定（同 Material.audited_by 先例），映射定案后回填
            comment=reason,
        )
    return material, kind


# ---------------------------------------------------------------- 软删

def material_remove(material_id: str, version: int, account) -> None:
    """软删（is_deleted+deleted_at），不物理删除；不限制当前审核状态。

    前端仅传 ?version=，多态由 _get_material_any 双表解析。
    """
    scope = _material_scope(account)
    from apps.persons.services import ensure_version, parse_path_id

    material, _kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_owner_in_scope(
        material.person if _kind == "PERSON" else material.team, scope
    )
    ensure_version(material, version)
    material.is_deleted = True
    material.deleted_at = timezone.now()
    material.save()


# ---------------------------------------------------------------- 详情

def material_get(material_id: str, account) -> tuple:
    """材料详情（批次 D-2）：多态双表解析，口径与 review/remove 完全一致。

    仅读取：软删行不可见（_active_queryset 同过滤规则）；两表同 id 有效行
    → 409 STATE_CONFLICT（归属不明确，不静默择一）；数据范围同列表
    （P1-9：全局 ADMIN 全库 / UNIVERSITY 本校，越权显式 403）。
    """
    scope = _material_scope(account)
    from apps.persons.services import parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_owner_in_scope(
        material.person if kind == "PERSON" else material.team, scope
    )
    return material, kind


# ---------------------------------------------------------------- 审核历史

def material_audit_logs(material_id: str, account):
    """审核历史查询（P1-9 原定任务，2026-10-28 统一业务规则批准补齐）。

    多态双表解析与数据范围同 material_get（全局 ADMIN 全库 / UNIVERSITY
    本校，越权显式 403）；返回 (logs, kind)，logs 按创建时间正序（早→晚，
    与状态机演进方向一致）。append-only 历史，仅读取不派生不聚合。
    """
    scope = _material_scope(account)
    from apps.persons.services import parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_owner_in_scope(
        material.person if kind == "PERSON" else material.team, scope
    )
    logs = MaterialAuditLog.objects.filter(
        material_kind=kind, material_id=material.pk
    ).order_by("created_at", "id")
    return logs, kind


# ---------------------------------------------------------------- 更新

def material_update(material_id: str, payload: dict, account) -> tuple:
    """更新材料（批次 D-3）：title→remark + 文件替换；多态解析同 review/remove。

    - 不可变字段（本批次规则）：ownerType/ownerId/materialType 与当前记录
      不一致 → 400 明确拒绝，本接口不支持变更归属或类型，不静默忽略；
    - fileIds 冻结单文件（schema 硬限 + 服务层防御复查）；换文件按 _resolve_file
      既有规则校验（存在 404/归属类型不符 409/已绑他属 409），未绑文件按
      先传后绑原子认领（同创建，竞态败出 409 整体回滚）；旧文件不解绑
      （不破坏其他材料的文件绑定）；R6 唯一性状态不因更新改变（归属与类型
      不可变，行数不变），不做重复检查；
    - 契约更新请求无 fileName 字段：换文件时 file_name 以 FileRecord.original_name
      为真实来源，不沿用旧名冒充新文件；
    - FileFileVersion 追加 v(n+1) 留痕（模型设计即"替换留痕，版本号替换+1"）；
    - 仅 PENDING 可更新（T4-04-L13，2026-10-24 用户批准）：非 PENDING
      （APPROVED/REJECTED 等）→ 409 STATE_CONFLICT；拒绝发生在任何业务
      写入之前，不触达文件绑定/版本留痕/审计路径，不发生状态转换；
    - version 乐观锁（B4）：ensure_version → 409 VERSION_CONFLICT；
    - 更新与 MaterialAuditLog 同事务（同审核修复③原子性口径）：材料行/文件
      认领/版本留痕/审计历史成败一体，审计落库失败整体回滚，不留部分更新
      或孤立审计记录；审计 from_status=to_status=当前状态（非状态变更操作
      历史），comment 记录更新动作。
    """
    scope = _material_scope(account)
    from apps.persons.services import ensure_version, parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_owner_in_scope(
        material.person if kind == "PERSON" else material.team, scope
    )
    # T4-04-L13（2026-10-24 用户批准）：仅 PENDING 可更新。存在性/软删/双表
    # 冲突检查已完成，拒绝发生在任何业务写入之前（早于归属校验、乐观锁、
    # 文件认领、版本留痕与审计），与 materialReview 的 PENDING 门槛同口径。
    if material.status != enums.MaterialStatus.PENDING:
        raise ApiError(
            STATE_CONFLICT,
            f"仅待审核状态的材料可修改（当前状态：{material.status}）",
        )
    owner_pk = material.person_id if kind == "PERSON" else material.team_id

    if payload["ownerType"] != kind:
        raise ApiError(
            INVALID_ARGUMENT,
            "材料归属类型不可变更",
            [{"field": "ownerType", "reason": "与当前记录不一致，本接口不支持变更归属或类型"}],
        )
    if payload["ownerId"] != str(owner_pk):
        raise ApiError(
            INVALID_ARGUMENT,
            "材料归属对象不可变更",
            [{"field": "ownerId", "reason": "与当前记录不一致，本接口不支持变更归属或类型"}],
        )
    if payload["materialType"] != material.type.code:
        raise ApiError(
            INVALID_ARGUMENT,
            "材料类型不可变更",
            [{"field": "materialType", "reason": "与当前记录不一致，本接口不支持变更归属或类型"}],
        )

    ensure_version(material, payload["version"])

    file_ids = payload.get("fileIds") or []
    if len(file_ids) != 1:
        raise ApiError(
            INVALID_ARGUMENT,
            "fileIds 仅支持 1 个文件（冻结模型为单文件承载）",
            [{"field": "fileIds", "reason": "必须恰好 1 个"}],
        )
    record = _resolve_file(file_ids[0], kind, owner_pk)
    file_changed = record.pk != material.file_id

    with transaction.atomic():
        material.remark = payload["title"]
        if file_changed:
            material.file_id = record.pk
            material.file_name = record.original_name
            material.file_size = record.size_bytes or None
            material.file_ext = _extension(record.original_name)
        material.save()
        if file_changed:
            if not record.owner_id:  # 先传后绑：原子认领（同创建链路，防并发双绑）
                claimed = FileRecord.objects.filter(pk=record.pk, owner_id="").update(
                    owner_id=str(owner_pk)
                )
                if claimed != 1:
                    raise ApiError(
                        STATE_CONFLICT,
                        "材料文件归属已被其他操作抢先绑定，未能取得该文件的绑定权，"
                        "本次更新已取消，请刷新后重试",
                    )
            last = (
                MaterialFileVersion.objects.filter(
                    material_kind=kind, material_id=material.pk
                )
                .order_by("-version")
                .first()
            )
            try:
                MaterialFileVersion.objects.create(
                    material_kind=kind,
                    material_id=material.pk,
                    version=(last.version + 1) if last else 1,
                    file_id=record.pk,
                    file_name=material.file_name,
                )
            except IntegrityError:
                # uk_mfv_version(material_kind, material_id, version) 冲突：并发更新
                # 同材料换文件时双双读到同一 last 版本，后提交者撞唯一约束。
                # 仅转译唯一约束冲突（其他数据库异常原样传播）；ApiError 传出
                # atomic 块 → 整个更新事务回滚（材料行/文件认领/审计成败一体），
                # 转译点不执行任何后续数据库操作。客户端刷新 version 后重试。
                raise ApiError(
                    STATE_CONFLICT,
                    "材料文件版本已被其他操作抢先更新，本次修改已取消，请刷新后重试",
                )
        # 审计记录与更新同事务：落库失败 → 材料行/文件认领/版本留痕整体回滚
        MaterialAuditLog.objects.create(
            material_kind=kind,
            material_id=material.pk,
            from_status=material.status,
            to_status=material.status,
            # auditor 恒空：底座账号 ID 映射未定（同上/同 Material.audited_by 先例）
            comment=f"更新材料: {material.remark}",
        )
    return material, kind


def _get_material(kind: str, material_id: int):
    """按 material_kind 取有效（未软删）材料，供审核/软删共用。"""
    material = (
        _active_queryset(kind)
        .select_related("person" if kind == "PERSON" else "team", "type")
        .filter(pk=material_id)
        .first()
    )
    if material is None:
        raise ApiError(NOT_FOUND, "材料不存在或已删除")
    return material


def _get_material_any(material_id: int) -> tuple:
    """前端 review/remove 仅传 id 不传归属类型 → 双表解析多态。

    两表主键独立自增存在同 id 可能：分别查询两表（仅未软删行，与列表同一
    过滤规则）并识别命中数量——仅一表命中沿用该行；两表同时命中属非预期
    数据关系，归属类型不明确 → 409 STATE_CONFLICT（不静默择一，防止误更新/
    误删错误对象）；两表均未命中 404。
    联调待办 L11：建议前端 review/remove 补传 ownerType 消除歧义。
    """
    person = (
        _active_queryset("PERSON")
        .select_related("person", "type")
        .filter(pk=material_id)
        .first()
    )
    team = (
        _active_queryset("TEAM")
        .select_related("team", "type")
        .filter(pk=material_id)
        .first()
    )
    if person is not None and team is not None:
        raise ApiError(
            STATE_CONFLICT,
            f"材料 ID {material_id} 同时存在于人员材料与球队材料中，"
            "需要明确归属类型后才能执行该操作",
        )
    if person is not None:
        return person, "PERSON"
    if team is not None:
        return team, "TEAM"
    raise ApiError(NOT_FOUND, "材料不存在或已删除")
