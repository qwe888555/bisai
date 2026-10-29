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
- 数据范围（2026-10-27 交付）：
  列表类接口按可见归属过滤（全局管理员全量、PERSON 走 persons.apply_person_scope、
  TEAM 走 teams.visible_teams），范围为空返回空列表（空态，不 403）；
  详情/写操作先解析归属对象（不存在/软删 404），再 ensure_owner_visible
  （越界 403 DATA_SCOPE_DENIED）。
"""

from __future__ import annotations

from itertools import chain
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Q
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
from apps.common.scopes import is_global_admin
from apps.materials.models import (
    MaterialAuditLog,
    MaterialFileVersion,
    MaterialType,
    PersonMaterial,
    TeamMaterial,
)
from apps.persons.models import Person
from apps.teams.models import Team


def ensure_owner_visible(account, owner_type: str, owner_id: str):
    """材料归属集中校验：归属对象可见才能读写其材料。

    - 归属对象不存在或已软删 → 404（沿用 _resolve_owner 语义）；
    - PERSON：复用 persons.apply_person_scope（全局管理员全量、TEAM 本队成员、
      ATHLETE/COACH 本人、UNIVERSITY/CITY/PROVINCE 学校 code 子树），
      不在范围 → 403 DATA_SCOPE_DENIED；
    - TEAM：复用 teams.ensure_team_visible（本队/本校范围），越界 403。
    返回解析到的归属对象（Person/Team），供调用方复用主键。
    """
    owner = _resolve_owner(owner_type, owner_id)
    if owner_type == "PERSON":
        from apps.persons.services import apply_person_scope

        if not apply_person_scope(Person.objects.filter(pk=owner.pk), account).exists():
            raise ApiError(DATA_SCOPE_DENIED, "人员不在当前账号数据范围内")
        return owner
    from apps.teams.services import ensure_team_visible

    ensure_team_visible(account, owner)
    return owner


def _scope_ids(queryset) -> list:
    return list(queryset.values_list("pk", flat=True))


def visible_materials(account, queryset):
    """列表类接口的归属范围过滤（queryset 为 PersonMaterial/TeamMaterial 单表）。

    全局管理员不过滤；其余按归属类型取账号可见归属对象 ID 集合并用 Q 过滤
    （两表分别承载 PERSON/TEAM 归属，故 owner_type 分流落在各自表上：
    `Q(person_id__in=人员范围)` / `Q(team_id__in=球队范围)`；归属列即模型 FK，
    与 persons/teams 主键同为 int，无需字符串转换）。角色范围为空
    （未绑定组织/无可见学校）→ 空集，列表正常显示空态，不 403。
    """
    if is_global_admin(account):
        return queryset
    if queryset.model is PersonMaterial:
        from apps.persons.services import apply_person_scope

        try:
            person_ids = _scope_ids(apply_person_scope(Person.objects.all(), account))
        except ApiError as exc:
            if exc.code != DATA_SCOPE_DENIED:
                raise
            person_ids = []
        return queryset.filter(Q(person_id__in=person_ids))

    from apps.teams.services import visible_teams

    try:
        team_ids = _scope_ids(visible_teams(account))
    except ApiError as exc:
        if exc.code != DATA_SCOPE_DENIED:
            raise
        team_ids = []
    return queryset.filter(Q(team_id__in=team_ids))


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
    """类型必须存在于 material_type 表且启用且归属匹配（applies_to 支持
    PERSON,TEAM 双归属；空表 → 400 明确报错）。

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
    if owner_type not in [p.strip() for p in mtype.applies_to.split(",") if p.strip()]:
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
    数据范围按可见归属过滤（visible_materials），越界归属返回空结果而非 403。
    返回 (items, kind) 元组列表，供序列化层携带多态 kind。
    """
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
            visible_materials(account, _active_queryset(kind))
            .select_related("person" if kind == "PERSON" else "team", "type")
            .filter(**({"person_id": owner_pk} if kind == "PERSON" else {"team_id": owner_pk}))
            .order_by("-created_at", "-id")
        )
        return [(m, kind) for m in rows]
    person_rows = visible_materials(account, _active_queryset("PERSON")).select_related(
        "person", "type"
    )
    team_rows = visible_materials(account, _active_queryset("TEAM")).select_related(
        "team", "type"
    )
    merged = sorted(
        chain(person_rows, team_rows),
        key=lambda m: (m.created_at, m.pk),
        reverse=True,
    )
    return [(m, "PERSON" if isinstance(m, PersonMaterial) else "TEAM") for m in merged]


# ---------------------------------------------------------------- 创建

def material_create(payload: dict, account) -> tuple:
    """创建材料：归属校验 → 类型校验 → 文件校验 → R6 → 状态落库。

    事务内完成：材料行 + 文件归属回填（先传后绑）+ MaterialFileVersion v1 留痕。
    返回 (material, kind)。
    """
    owner_type = payload["ownerType"]
    owner = ensure_owner_visible(account, owner_type, payload["ownerId"])
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


def _ensure_material_visible(account, material, kind: str) -> None:
    """详情/写操作：按材料归属对象校验数据范围（越界 403、归属不存在 404）。"""
    owner_pk = material.person_id if kind == "PERSON" else material.team_id
    ensure_owner_visible(account, kind, str(owner_pk))


# ---------------------------------------------------------------- 审核

def material_review(material_id: str, payload: dict, account) -> tuple:
    """审核：仅 PENDING 可处理；APPROVE/REJECT → APPROVED/REJECTED + 审核历史。

    前端 body 无归属类型字段，多态由 _get_material_any 双表解析；
    角色（ADMIN/PROVINCE_REVIEWER/CITY）之外的扩展不在此处，数据范围按
    材料归属校验（越界 403）。
    """
    from apps.persons.services import ensure_version, parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_material_visible(account, material, kind)
    ensure_version(material, payload["version"])
    if material.status != enums.MaterialStatus.PENDING:
        raise ApiError(
            STATE_CONFLICT,
            f"材料当前状态为 {material.status}，仅待审核（PENDING）材料可处理",
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
    with transaction.atomic():
        material.save()
        MaterialAuditLog.objects.create(
            material_kind=kind,
            material_id=material.pk,
            from_status=from_status,
            to_status=material.status,
            comment=reason,
        )
    return material, kind


# ---------------------------------------------------------------- 软删

def material_remove(material_id: str, version: int, account) -> None:
    """软删（is_deleted+deleted_at），不物理删除；不限制当前审核状态。

    前端仅传 ?version=，多态由 _get_material_any 双表解析。
    数据范围按材料归属校验（越界 403）。
    """
    from apps.persons.services import ensure_version, parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_material_visible(account, material, kind)
    ensure_version(material, version)
    material.is_deleted = True
    material.deleted_at = timezone.now()
    material.save()


# ---------------------------------------------------------------- 详情

def material_get(material_id: str, account) -> tuple:
    """材料详情（批次 D-2）：多态双表解析，口径与 review/remove 完全一致。

    仅读取：软删行不可见（_active_queryset 同过滤规则）；两表同 id 有效行
    → 409 STATE_CONFLICT（归属不明确，不静默择一）；数据范围按材料归属校验
    （越界 403，归属对象不存在/软删 404）。
    """
    from apps.persons.services import parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    _ensure_material_visible(account, material, kind)
    return material, kind


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
    from apps.persons.services import ensure_version, parse_path_id

    material, kind = _get_material_any(parse_path_id(material_id, "materialId"))
    # 数据范围先于状态判定：越界账号不因状态分支错误码泄露材料状态（403）
    _ensure_material_visible(account, material, kind)
    # T4-04-L13（2026-10-24 用户批准）：仅 PENDING 可更新。存在性/软删/双表
    # 冲突与归属范围检查已完成，状态拒绝发生在任何业务写入之前（早于乐观锁、
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
