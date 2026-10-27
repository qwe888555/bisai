"""外部依赖客户端（成员3 的隔离层）。

每个外部依赖一个函数族：
- file_client：真实对接 apps.attachments（不签直链、不直连 MinIO）；
- team_client / person_client / material_client / event_config_client / message_client：
  默认走 mock（ENROLLMENT_MOCK_CLIENTS=true），返回 deterministic 假数据；
  联调期把开关置 false 并替换为真实接口。

环境变量在模块内直读（common/conf.py 归成员2，不改）。
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

from django.utils import timezone

from apps.common.crypto import (
    mask_id_number,
    mask_phone,
    mask_student_number,
    normalize_id_number,
)

logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on"}


def mock_enabled() -> bool:
    return os.environ.get("ENROLLMENT_MOCK_CLIENTS", "true").strip().lower() in TRUE_VALUES


def _gender_label(value) -> str:
    """Person.gender (int 0/1/2) → 契约标签。"""
    from apps.common.enums import Gender

    mapping = {Gender.MALE: "MALE", Gender.FEMALE: "FEMALE", Gender.UNKNOWN: "UNKNOWN"}
    try:
        return mapping.get(int(value), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


def _resolve_person(athlete_id: str):
    """athlete_id 可能是 Person.pk 或 person_no，统一解析为 Person 实例。"""
    from apps.persons.models import Person

    if not athlete_id:
        return None
    try:
        person = Person.objects.filter(pk=int(athlete_id), is_deleted=False).first()
    except (TypeError, ValueError):
        person = None
    if person is None:
        person = Person.objects.filter(person_no=athlete_id, is_deleted=False).first()
    return person


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _suffix(value: str) -> str:
    return _digest(value)[:4]


# ---------------------------------------------------------------- 文件


class FileClient:
    """真实文件服务：只做归属校验与落库，不签名直链。"""

    def get_record(self, account, file_id: str):
        from apps.attachments import services as attachment_services

        return attachment_services.get_record(account, file_id)

    def can_access(self, record, account) -> bool:
        from apps.attachments import services as attachment_services

        return attachment_services.can_access(record, account)

    def upload(self, account, owner_type: str, owner_id: str, category: str, uploaded):
        from apps.attachments import services as attachment_services

        return attachment_services.upload(
            account, owner_type, owner_id, category, uploaded
        )


file_client = FileClient()


# ---------------------------------------------------------------- 球队


_TEAM_SEED: dict[str, dict[str, Any]] = {
    "org_team_01": {
        "id": "org_team_01",
        "name": "示例第一中学男子队",
        "orgId": "org_school_01",
        "cityOrgId": "org_city_01",
        "cityName": "示例市一",
        "schoolCode": "SCH001",
        "schoolName": "示例第一中学",
        "coachIds": ["coach_01", "coach_02"],
    },
    "org_team_02": {
        "id": "org_team_02",
        "name": "示例第二中学女子队",
        "orgId": "org_school_02",
        "cityOrgId": "org_city_02",
        "cityName": "示例市二",
        "schoolCode": "SCH002",
        "schoolName": "示例第二中学",
        "coachIds": ["coach_03"],
    },
    "org_univ_01": {
        "id": "org_univ_01",
        "name": "示例大学足球队",
        "orgId": "org_univ_01",
        "cityOrgId": "org_prov",
        "cityName": "省直属",
        "schoolCode": "UNI001",
        "schoolName": "示例大学",
        "coachIds": ["coach_u1"],
    },
}


class TeamClient:
    @staticmethod
    def _real_team(team_id: str) -> Optional[dict[str, Any]]:
        """优先读本库真实球队主档，读不到再走演示数据。"""
        try:
            from apps.accounts.models import Organization
            from apps.teams.models import Team, TeamCoach

            team = Team.objects.filter(pk=str(team_id), is_deleted=False).first()
            if team is None:
                return None
            school = team.school
            region = team.region
            city_org = None
            if region is not None:
                city_org = (
                    Organization.objects.filter(org_type="CITY")
                    .filter(code=region.code)
                    .first()
                    or Organization.objects.filter(org_type="CITY", name=region.name).first()
                )
            school_org = None
            if school is not None and school.code:
                school_org = Organization.objects.filter(code=school.code).first()
            coach_ids = [
                str(person_id)
                for person_id in TeamCoach.objects.filter(team=team).values_list(
                    "person_id", flat=True
                )
            ]
            return {
                "id": str(team.pk),
                "name": team.name,
                "orgId": school_org.pk if school_org else (str(school.pk) if school else ""),
                "cityOrgId": city_org.pk if city_org else "",
                "cityName": region.name if region else "",
                "schoolCode": (school.code if school and school.code else ""),
                "schoolName": school.name if school else "",
                "coachIds": coach_ids,
            }
        except Exception:  # pragma: no cover - 演示兜底，任何异常都退回 mock
            logger.debug("读取真实球队失败，回退演示数据", exc_info=True)
            return None

    def get_team(self, team_id: str) -> dict[str, Any]:
        if not team_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少球队 ID")
        if mock_enabled():
            real = self._real_team(team_id)
            if real is not None:
                return real
            seed = _TEAM_SEED.get(team_id)
            if seed:
                return dict(seed)
            city = "org_city_02" if _suffix(team_id)[0] in "89abcdef" else "org_city_01"
            return {
                "id": team_id,
                "name": f"示例球队{_suffix(team_id)}",
                "orgId": team_id,
                "cityOrgId": city,
                "cityName": "示例市",
                "schoolCode": f"SCH{_suffix(team_id)}",
                "schoolName": "示例学校",
                "coachIds": [f"coach_{_suffix(team_id)}"],
            }
        from apps.common.errors import ApiError, NOT_FOUND
        from apps.accounts.models import Organization
        from apps.teams.models import Team, TeamCoach

        try:
            team = Team.objects.get(pk=team_id, is_deleted=False)
        except (Team.DoesNotExist, TypeError, ValueError):
            raise ApiError(NOT_FOUND, "球队主档不存在")
        org = Organization.objects.filter(code=team.team_no).first()
        school = team.school
        region = team.region
        coach_ids = list(
            TeamCoach.objects.filter(team=team, status="CURRENT").values_list(
                "person_id", flat=True
            )
        )
        return {
            "id": str(team.pk),
            "name": team.name,
            "orgId": str(org.pk) if org else str(team.pk),
            "cityOrgId": "",
            "cityName": region.name if region else "",
            "schoolCode": school.code if school else "",
            "schoolName": school.name if school else "",
            "coachIds": [str(pk) for pk in coach_ids],
        }

    def team_ids_for_org(self, org_id: str) -> list[str]:
        """账号组织范围内可报送的球队 ID。"""
        if not org_id:
            return []
        if mock_enabled():
            mapping = {
                "org_team_01": ["org_team_01"],
                "org_team_02": ["org_team_02"],
                "org_school_01": ["org_team_01"],
                "org_school_02": ["org_team_02"],
                "org_city_01": ["org_team_01"],
                "org_city_02": ["org_team_02"],
                "org_univ_01": ["org_univ_01"],
                "org_prov": ["org_team_01", "org_team_02", "org_univ_01"],
            }
            ids = list(mapping.get(org_id, [org_id]))
            # mock 叠加真实主档：Organization.code == Team.team_no（唯一），
            # 球队 org 节点的 parent 即报送主体，子树上挂的真实球队一并放行
            ids.extend(self._real_team_ids(org_id))
            return ids
        from apps.accounts.models import Organization
        from apps.teams.models import Team

        try:
            org = Organization.objects.get(pk=org_id)
        except (Organization.DoesNotExist, TypeError, ValueError):
            return []
        if org.org_type == "TEAM":
            team = Team.objects.filter(team_no=org.code, is_deleted=False).first()
            return [str(team.pk)] if team else []
        if org.org_type in ("SCHOOL", "UNIVERSITY"):
            codes = list(
                Organization.objects.filter(parent_id=org.pk, org_type="TEAM").values_list(
                    "code", flat=True
                )
            )
            if not codes:
                return []
            return [
                str(pk)
                for pk in Team.objects.filter(
                    team_no__in=codes, is_deleted=False
                ).values_list("pk", flat=True)
            ]
        # CITY / PROVINCE / ADMIN：省/管理员可见全部；
        # 市州级细化隔离依赖成员4 暴露 org→region 映射，待补
        logger.warning(
            "team_ids_for_org 对 %s 返回全量球队（市州隔离待成员4 补 org→region 映射）",
            org.org_type,
        )
        return [
            str(pk)
            for pk in Team.objects.filter(is_deleted=False, status="ACTIVE").values_list(
                "pk", flat=True
            )
        ]

    @staticmethod
    def _real_team_ids(org_id: str) -> list[str]:
        """球队 org 节点 parent=org_id 的真实 Team 主档 PK（mock 兜底，异常降级为空）。

        球队账号自身绑定的就是 TEAM 组织节点，其 code 即 Team.team_no，
        因此也要把本节点映射出的球队算进来，否则本队报名单不可见。
        """
        try:
            from apps.accounts.models import Organization
            from apps.teams.models import Team

            codes = list(
                Organization.objects.filter(org_type="TEAM", parent_id=org_id).values_list(
                    "code", flat=True
                )
            )
            own_code = (
                Organization.objects.filter(pk=org_id, org_type="TEAM")
                .values_list("code", flat=True)
                .first()
            )
            if own_code:
                codes.append(own_code)
            if not codes:
                return []
            return [
                str(pk)
                for pk in Team.objects.filter(
                    team_no__in=codes, is_deleted=False
                ).values_list("pk", flat=True)
            ]
        except Exception:
            return []

    def get_coach(self, coach_id: str) -> dict[str, Any]:
        if mock_enabled():
            tail = _suffix(coach_id)
            return {
                "id": coach_id,
                "name": f"教练{tail}",
                "role": "主教练",
                "maskedPhone": mask_phone(f"1380000{tail[:4]}"),
            }
        from apps.common.errors import ApiError, NOT_FOUND
        from apps.persons.models import Person

        try:
            person = Person.objects.get(
                pk=coach_id, person_category="COACH", is_deleted=False
            )
        except (Person.DoesNotExist, TypeError, ValueError):
            raise ApiError(NOT_FOUND, "教练主档不存在")
        return {
            "id": coach_id,
            "name": person.full_name,
            "role": "主教练",
            "maskedPhone": person.phone_masked or "",
        }


team_client = TeamClient()


# ---------------------------------------------------------------- 人员


_BASE_DATE = date(2010, 1, 1)


class PersonClient:
    def get_person(self, athlete_id: str) -> dict[str, Any]:
        if not athlete_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少运动员 ID")
        if mock_enabled():
            tail = _suffix(athlete_id)
            return {
                "id": athlete_id,
                "name": f"运动员{tail[:2]}",
                "maskedIdNumber": mask_id_number(f"11010120100101{tail[:4]}"),
                "gender": "MALE",
                "birthDate": _BASE_DATE.isoformat(),
                "maskedStudentNumber": mask_student_number(f"ST{tail}0001"),
                "hasIdConflict": False,
                "banned": False,
            }
        from apps.common.errors import ApiError, NOT_FOUND

        person = _resolve_person(athlete_id)
        if person is None:
            raise ApiError(NOT_FOUND, "运动员主档不存在，请先在人员系统建档")
        student_no = ""
        try:
            student_no = person.athlete_profile.student_no or ""
        except Exception:  # noqa: BLE001 - 无扩展档案时退化为空
            student_no = ""
        return {
            "id": str(person.pk),
            "name": person.full_name,
            "maskedIdNumber": person.id_card_masked or "",
            "gender": _gender_label(person.gender),
            "birthDate": person.birth_date.isoformat() if person.birth_date else "",
            "maskedStudentNumber": mask_student_number(student_no) if student_no else "",
            "hasIdConflict": False,
            "banned": False,
        }

    def get_persons(self, athlete_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {athlete_id: self.get_person(athlete_id) for athlete_id in athlete_ids}

    def resolve_person(
        self,
        name: str,
        id_number: str,
        *,
        student_number: str = "",
        gender: str = "",
        birth_date: str = "",
        school_code: str = "",
    ) -> dict[str, Any]:
        """导入时按身份证匹配/新建人员主档（真实实现由成员4 提供）。"""
        if mock_enabled():
            normalized = normalize_id_number(id_number)
            person_id = "per_" + _digest(normalized)[:20]
            return {
                "id": person_id,
                "name": name,
                "maskedIdNumber": mask_id_number(normalized),
                "gender": gender,
                "birthDate": birth_date,
                "maskedStudentNumber": mask_student_number(student_number),
                "created": True,
            }
        # 真实建档/匹配由成员4 人员服务提供（冻结期不在本模块写主档，
        # 且身份证哈希含盐无法在本模块复算）。导入新人员请走成员4 接口。
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(
            DEPENDENCY_UNAVAILABLE,
            "人员主档匹配/新建依赖成员4 人员服务，本适配器不直接写主档",
        )


person_client = PersonClient()


# ---------------------------------------------------------------- 履历材料


class MaterialClient:
    def list_materials(self, owner_type: str, owner_id: str) -> list[dict[str, Any]]:
        """查询人员/球队履历材料；mock 返回空列表表示"取不到"。

        取不到时预检按 QUALIFICATION_PENDING WARNING 处理，不臆造缺失。
        """
        if mock_enabled():
            return []
        from apps.materials.models import PersonMaterial, TeamMaterial

        try:
            if owner_type == "TEAM":
                rows = TeamMaterial.objects.filter(
                    team_id=owner_id, is_deleted=False
                ).select_related("type")
            else:
                person = _resolve_person(owner_id)
                if person is None:
                    return []
                rows = PersonMaterial.objects.filter(
                    person_id=person.pk, is_deleted=False
                ).select_related("type")
            return [
                {
                    "type": m.type.code if m.type else "",
                    "fileId": m.file_id,
                    "fileName": m.file_name,
                    "status": m.status,
                }
                for m in rows
            ]
        except Exception:  # noqa: BLE001 - 取不到材料只降级空列表，不阻断校验/导出
            logger.exception(
                "材料服务查询异常 owner_type=%s owner_id=%s", owner_type, owner_id
            )
            return []


material_client = MaterialClient()


# ---------------------------------------------------------------- 赛事配置


def _mock_window() -> tuple[datetime, datetime]:
    now = timezone.now()
    open_raw = os.environ.get("ENROLLMENT_MOCK_WINDOW_OPEN", "")
    close_raw = os.environ.get("ENROLLMENT_MOCK_WINDOW_CLOSE", "")
    opens = now - timedelta(days=30)
    closes = now + timedelta(days=30)
    if open_raw:
        opens = datetime.fromisoformat(open_raw)
        if timezone.is_naive(opens):
            opens = timezone.make_aware(opens)
    if close_raw:
        closes = datetime.fromisoformat(close_raw)
        if timezone.is_naive(closes):
            closes = timezone.make_aware(closes)
    return opens, closes


class EventConfigClient:
    @staticmethod
    def _real_event(event_id: str) -> Optional[dict[str, Any]]:
        try:
            from apps.events.models import Event

            event = Event.objects.filter(pk=str(event_id)).first()
            if event is None:
                return None
            return {
                "id": event.pk,
                "name": event.name,
                "year": event.season,
                "timezone": "Asia/Shanghai",
            }
        except Exception:  # pragma: no cover
            logger.debug("读取真实赛事失败，回退演示数据", exc_info=True)
            return None

    @staticmethod
    def _real_group(event_id: str, group_id: str) -> Optional[dict[str, Any]]:
        try:
            from apps.events.models import EventGroup

            group = EventGroup.objects.filter(
                pk=str(group_id), event_id=str(event_id)
            ).first()
            if group is None:
                group = EventGroup.objects.filter(pk=str(group_id)).first()
            if group is None:
                return None
            return {"id": group.pk, "name": group.name, "eventId": group.event_id}
        except Exception:  # pragma: no cover
            logger.debug("读取真实组别失败，回退演示数据", exc_info=True)
            return None

    def get_event(self, event_id: str) -> dict[str, Any]:
        if not event_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少赛事 ID")
        if mock_enabled():
            real = self._real_event(event_id)
            if real is not None:
                return real
            return {
                "id": event_id,
                "name": f"省级中小学足球赛事{_suffix(event_id)}",
                "year": timezone.now().year,
                "timezone": "Asia/Shanghai",
            }
        from apps.events.models import Event

        event = Event.objects.filter(pk=event_id).first()
        if event is None:
            logger.warning("赛事 %s 不存在，回退默认配置", event_id)
            return {
                "id": event_id,
                "name": f"赛事{event_id}",
                "year": timezone.now().year,
                "timezone": "Asia/Shanghai",
            }
        try:
            year = int(event.season)
        except (TypeError, ValueError):
            year = timezone.now().year
        return {
            "id": event.id,
            "name": event.name,
            "year": year,
            "timezone": "Asia/Shanghai",
        }

    def get_group(self, event_id: str, group_id: str) -> dict[str, Any]:
        if not group_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少组别 ID")
        if mock_enabled():
            real = self._real_group(event_id, group_id)
            if real is not None:
                return real
            return {
                "id": group_id,
                "name": f"组别{_suffix(group_id)}",
                "eventId": event_id,
            }
        from apps.events.models import EventGroup

        group = EventGroup.objects.filter(pk=group_id, event_id=event_id).first()
        if group is None:
            logger.warning("组别 %s(赛事 %s) 不存在，回退默认", group_id, event_id)
            return {"id": group_id, "name": f"组别{group_id}", "eventId": event_id}
        return {"id": group.id, "name": group.name, "eventId": group.event_id}

    def get_groups(self, event_id: str) -> list[dict[str, Any]]:
        if mock_enabled():
            try:
                from apps.events.models import EventGroup

                rows = EventGroup.objects.filter(event_id=str(event_id)).order_by("created_at")
                if rows.exists():
                    return [
                        {"id": item.pk, "name": item.name, "eventId": item.event_id}
                        for item in rows
                    ]
            except Exception:  # pragma: no cover
                logger.debug("读取真实组别列表失败，回退演示数据", exc_info=True)
            return [
                {"id": "group_u12", "name": "小学男子组", "eventId": event_id},
                {"id": "group_u15", "name": "初中男子组", "eventId": event_id},
            ]
        from apps.events.models import EventGroup

        return [
            {"id": g.id, "name": g.name, "eventId": g.event_id}
            for g in EventGroup.objects.filter(event_id=event_id)
        ]

    def get_window(self, event_id: str) -> dict[str, datetime]:
        if mock_enabled():
            opens, closes = _mock_window()
            return {"opensAt": opens, "closesAt": closes}
        from apps.events.models import Event

        event = Event.objects.filter(pk=event_id).first()
        if event is None:
            logger.warning("赛事 %s 不存在，回退默认窗口", event_id)
            opens, closes = _mock_window()
            return {"opensAt": opens, "closesAt": closes}
        return {"opensAt": event.registration_opens_at, "closesAt": event.registration_closes_at}

    def get_group_rules(self, event_id: str, group_id: str) -> dict[str, Any]:
        """组别资格参数。缺失的键表示"参数未确认"，预检归 QUALIFICATION_PENDING。"""
        if mock_enabled():
            return {
                "minPlayers": 5,
                "maxPlayers": 25,
                "minGoalkeepers": 1,
                "birthDateFrom": "2008-01-01",
                "birthDateTo": "2012-12-31",
                "requiredMaterialTypes": ["STAMPED_FORM", "STUDENT_STATUS"],
                "confirmed": True,
            }
        from apps.events.models import EventGroup

        group = EventGroup.objects.filter(pk=group_id, event_id=event_id).first()
        if group is None:
            logger.warning("组别 %s(赛事 %s) 不存在，回退未确认规则", group_id, event_id)
            return {
                "minPlayers": None,
                "maxPlayers": None,
                "minGoalkeepers": None,
                "birthDateFrom": None,
                "birthDateTo": None,
                "requiredMaterialTypes": [],
                "confirmed": False,
            }
        # 模型仅含 max_players / min_age / max_age / quota / required_material_types；
        # min_players / min_goalkeepers / 出生日期闭区间需成员5 配置或按年龄换算，
        # 缺失归 QUALIFICATION_PENDING（与预检降级逻辑一致）。
        return {
            "minPlayers": None,
            "maxPlayers": group.max_players,
            "minGoalkeepers": None,
            "birthDateFrom": None,
            "birthDateTo": None,
            "requiredMaterialTypes": list(group.required_material_types or []),
            "confirmed": True,
        }

    def get_submission_route(self, subject_type: str) -> list[str]:
        """默认契约：SCHOOL_TEAM 走市州→省级；UNIVERSITY/CITY 直省审。"""
        if subject_type == "SCHOOL_TEAM":
            return ["CITY", "PROVINCE"]
        return ["PROVINCE"]


event_config_client = EventConfigClient()


# ---------------------------------------------------------------- 站内信


class MessageClient:
    def notify_enrollment(self, enrollment, event: str, extra: Optional[str] = None) -> None:
        """站内信桩：成员2 的消息中心接通后替换。只记日志，不写隐私字段。"""
        logger.info(
            "报名消息事件 enrollment=%s event=%s status=%s %s",
            enrollment.pk,
            event,
            enrollment.status,
            extra or "",
        )


message_client = MessageClient()
