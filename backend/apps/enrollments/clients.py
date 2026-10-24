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
        "schoolCode": "SCH001",
        "schoolName": "示例第一中学",
        "coachIds": ["coach_01", "coach_02"],
    },
    "org_team_02": {
        "id": "org_team_02",
        "name": "示例第二中学女子队",
        "orgId": "org_school_02",
        "cityOrgId": "org_city_02",
        "schoolCode": "SCH002",
        "schoolName": "示例第二中学",
        "coachIds": ["coach_03"],
    },
    "org_univ_01": {
        "id": "org_univ_01",
        "name": "示例大学足球队",
        "orgId": "org_univ_01",
        "cityOrgId": "org_prov",
        "schoolCode": "UNI001",
        "schoolName": "示例大学",
        "coachIds": ["coach_u1"],
    },
}


class TeamClient:
    def get_team(self, team_id: str) -> dict[str, Any]:
        if not team_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少球队 ID")
        if mock_enabled():
            seed = _TEAM_SEED.get(team_id)
            if seed:
                return dict(seed)
            city = "org_city_02" if _suffix(team_id)[0] in "89abcdef" else "org_city_01"
            return {
                "id": team_id,
                "name": f"示例球队{_suffix(team_id)}",
                "orgId": team_id,
                "cityOrgId": city,
                "schoolCode": f"SCH{_suffix(team_id)}",
                "schoolName": "示例学校",
                "coachIds": [f"coach_{_suffix(team_id)}"],
            }
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "球队服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

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
            return list(mapping.get(org_id, [org_id]))
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "球队服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

    def get_coach(self, coach_id: str) -> dict[str, Any]:
        if mock_enabled():
            tail = _suffix(coach_id)
            return {
                "id": coach_id,
                "name": f"教练{tail}",
                "role": "主教练",
                "maskedPhone": mask_phone(f"1380000{tail[:4]}"),
            }
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "球队服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")


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
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "人员服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

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
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "人员服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")


person_client = PersonClient()


# ---------------------------------------------------------------- 履历材料


class MaterialClient:
    def list_materials(self, owner_type: str, owner_id: str) -> list[dict[str, Any]]:
        """查询人员/球队履历材料；mock 返回空列表表示"取不到"。

        取不到时预检按 QUALIFICATION_PENDING WARNING 处理，不臆造缺失。
        """
        if mock_enabled():
            return []
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "材料服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")


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
    def get_event(self, event_id: str) -> dict[str, Any]:
        if not event_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少赛事 ID")
        if mock_enabled():
            return {
                "id": event_id,
                "name": f"省级中小学足球赛事{_suffix(event_id)}",
                "year": timezone.now().year,
                "timezone": "Asia/Shanghai",
            }
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "赛事配置服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

    def get_group(self, event_id: str, group_id: str) -> dict[str, Any]:
        if not group_id:
            from apps.common.errors import INVALID_ARGUMENT, ApiError

            raise ApiError(INVALID_ARGUMENT, "缺少组别 ID")
        if mock_enabled():
            return {
                "id": group_id,
                "name": f"组别{_suffix(group_id)}",
                "eventId": event_id,
            }
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "赛事配置服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

    def get_groups(self, event_id: str) -> list[dict[str, Any]]:
        if mock_enabled():
            return [
                {"id": "group_u12", "name": "小学男子组", "eventId": event_id},
                {"id": "group_u15", "name": "初中男子组", "eventId": event_id},
            ]
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "赛事配置服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

    def get_window(self, event_id: str) -> dict[str, datetime]:
        if mock_enabled():
            opens, closes = _mock_window()
            return {"opensAt": opens, "closesAt": closes}
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "赛事配置服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

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
        from apps.common.errors import ApiError, DEPENDENCY_UNAVAILABLE

        raise ApiError(DEPENDENCY_UNAVAILABLE, "赛事配置服务尚未接通，请置 ENROLLMENT_MOCK_CLIENTS=true")

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
