"""Adapter between this module and member 4's registration archive.

Publishing never accepts arbitrary registration fields from the browser: it
asks this source for the authoritative record, then copies only the fields in
``SELECTION_PUBLICITY_FIELD_WHITELIST`` into the public snapshot.

The main project should implement :class:`PublicitySourceClient` on top of
member 4's athlete / coach / team registration tables and point
``SELECTION_PUBLICITY_SOURCE_CLIENT`` at a callable that returns it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from django.conf import settings
from django.utils.module_loading import import_string

from ..common import BusinessError
from ..enums import GroupType, PublicityType
from .archive import DemoSelectionArchiveClient


@dataclass(frozen=True)
class PublicitySourceRecord:
    """One raw registration record supplied by member 4's archive."""

    source_id: str
    event_id: str
    group_type: str
    city: str
    publicity_type: str
    data: Dict[str, object]


class PublicitySourceClient(ABC):
    """Minimal interface required by the publicity module."""

    @abstractmethod
    def get_registration(
        self,
        publicity_type: str,
        source_id: str,
        event_id: Optional[str] = None,
    ) -> Optional[PublicitySourceRecord]:
        """Return one registration or None when it does not exist."""

    def list_registrations(
        self,
        group_type: Optional[str] = None,
        city: Optional[str] = None,
        publicity_type: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> List[PublicitySourceRecord]:
        """Return registrations matching the optional filters."""
        return []


class DemoPublicitySourceClient(PublicitySourceClient):
    """Deterministic demo registrations: 45 athletes + 15 coaches + 9 teams.

    Every record contains a fake raw ID card plus fields that are deliberately
    outside the whitelist (phone, birth_date, contact_phone) so tests can prove
    they never reach the public snapshot.
    """

    CITIES = ["江城市", "南湖市", "东林市"]

    COACH_NAMES = [
        "陈志强",
        "刘建国",
        "王海峰",
        "李明远",
        "赵国庆",
        "周文斌",
        "吴志刚",
        "郑凯旋",
        "孙国栋",
        "马俊杰",
        "朱永康",
        "胡明辉",
        "郭立新",
        "何振华",
        "高远航",
    ]

    COACH_ROLES = ["主教练", "助理教练", "战术教练", "体能教练", "守门员教练"]

    AREA_CODES = ["510101", "510102", "510103"]

    GROUPS = [
        (GroupType.HIGHER, 1000, 0, 0),
        (GroupType.MIDDLE, 2000, 5, 1),
        (GroupType.PRIMARY, 3000, 10, 2),
    ]

    def __init__(self):
        self._records: Dict[tuple, PublicitySourceRecord] = {}
        self._build()

    @classmethod
    def _fake_id_card(cls, group_index, serial):
        area = cls.AREA_CODES[group_index]
        sequence = f"{group_index * 100 + serial:03d}"
        check = "0123456789X"[(group_index * 7 + serial) % 11]
        return f"{area}20000101{sequence}{check}"

    def _build(self):
        archive = DemoSelectionArchiveClient()

        for group_type, base_id, name_offset, group_index in self.GROUPS:
            candidates = archive.list_candidates(group_type)

            # Athletes: source_id is the same player-archive ID used by scoring.
            for index, candidate in enumerate(candidates):
                city = self.CITIES[index % len(self.CITIES)]
                source_id = str(candidate.candidate_id)
                self._records[(PublicityType.ATHLETE, source_id)] = (
                    PublicitySourceRecord(
                        source_id=source_id,
                        event_id=candidate.event_id,
                        group_type=group_type,
                        city=city,
                        publicity_type=PublicityType.ATHLETE,
                        data={
                            "name": candidate.name,
                            "gender": "男" if index % 2 == 0 else "女",
                            "id_card": self._fake_id_card(group_index, index + 1),
                            "team_name": candidate.team_name,
                            "school_name": candidate.team_name,
                            "position": candidate.position,
                            "jersey_number": index + 1,
                            # Deliberately not whitelisted:
                            "phone": f"1380000{group_index}{index:02d}",
                            "birth_date": "2000-01-01",
                        },
                    )
                )

            teams = archive.GROUP_TEAMS[group_type]

            # Teams: T1001/T1002/T1003, T2001...
            for index, team_name in enumerate(teams):
                source_id = f"T{base_id + index + 1}"
                self._records[(PublicityType.TEAM, source_id)] = (
                    PublicitySourceRecord(
                        source_id=source_id,
                        event_id="1",
                        group_type=group_type,
                        city=self.CITIES[index % len(self.CITIES)],
                        publicity_type=PublicityType.TEAM,
                        data={
                            "team_name": team_name,
                            "school_name": team_name,
                            "coach_name": self.COACH_NAMES[
                                name_offset + index
                            ],
                            "team_type": "学校代表队",
                            "contact_phone": f"1390000{group_index}{index:02d}",
                        },
                    )
                )

            # Coaches: C1001-C1005, C2001...
            for index in range(5):
                source_id = f"C{base_id + index + 1}"
                team_name = teams[index % len(teams)]
                self._records[(PublicityType.COACH, source_id)] = (
                    PublicitySourceRecord(
                        source_id=source_id,
                        event_id="1",
                        group_type=group_type,
                        city=self.CITIES[index % len(self.CITIES)],
                        publicity_type=PublicityType.COACH,
                        data={
                            "name": self.COACH_NAMES[name_offset + index],
                            "gender": "男" if index % 2 == 0 else "女",
                            "id_card": self._fake_id_card(
                                group_index, 20 + index
                            ),
                            "team_name": team_name,
                            "school_name": team_name,
                            "coach_role": self.COACH_ROLES[index],
                            "phone": f"1370000{group_index}{index:02d}",
                        },
                    )
                )

    def get_registration(
        self,
        publicity_type: str,
        source_id: str,
        event_id: Optional[str] = None,
    ) -> Optional[PublicitySourceRecord]:
        try:
            code = PublicityType.parse(publicity_type)
        except BusinessError:
            return None
        record = self._records.get((code, str(source_id)))
        if record is not None and event_id is not None:
            if record.event_id != str(event_id):
                return None
        return record

    def list_registrations(
        self,
        group_type: Optional[str] = None,
        city: Optional[str] = None,
        publicity_type: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> List[PublicitySourceRecord]:
        type_code = None
        if publicity_type:
            type_code = PublicityType.parse(publicity_type)
        city_text = (city or "").strip()
        event_text = str(event_id) if event_id is not None else None

        return [
            record
            for record in self._records.values()
            if (
                (not group_type or record.group_type == group_type)
                and (not city_text or record.city == city_text)
                and (not type_code or record.publicity_type == type_code)
                and (event_text is None or record.event_id == event_text)
            )
        ]


_SOURCE_CLIENT_CACHE = {}


def get_publicity_source_client() -> PublicitySourceClient:
    """Resolve the configured registration source, with instance caching.

    ``auto`` uses the main project's member4 tables when they are installed,
    and falls back to the deterministic demo source in standalone mode.
    """
    mode = getattr(settings, "SELECTION_PUBLICITY_SOURCE_MODE", "auto")
    path = getattr(settings, "SELECTION_PUBLICITY_SOURCE_CLIENT", "")
    key = (mode, path)

    if key in _SOURCE_CLIENT_CACHE:
        return _SOURCE_CLIENT_CACHE[key]

    if path:
        try:
            factory = import_string(path)
            client = factory()
        except Exception as exc:
            raise BusinessError(
                500,
                "报名公示数据源初始化失败，请检查 "
                f"SELECTION_PUBLICITY_SOURCE_CLIENT: {exc}",
            )
        if not hasattr(client, "get_registration"):
            raise BusinessError(
                500,
                "报名公示数据源必须实现 "
                "get_registration(publicity_type, source_id)",
            )
    elif mode == "demo":
        client = DemoPublicitySourceClient()
    elif mode in ("auto", "main"):
        from .main_adapters import main_data_available
        from .main_adapters import MainProjectPublicitySourceClient

        if main_data_available():
            client = MainProjectPublicitySourceClient()
        elif mode == "auto":
            client = DemoPublicitySourceClient()
        else:
            raise BusinessError(
                500,
                "SELECTION_PUBLICITY_SOURCE_MODE=main，但主程序 "
                "apps.enrollments/apps.persons/apps.teams/apps.events 未安装",
            )
    else:
        raise BusinessError(
            500,
            "未配置报名公示数据源，请设置 "
            "SELECTION_PUBLICITY_SOURCE_MODE=auto/demo 或提供 "
            "SELECTION_PUBLICITY_SOURCE_CLIENT",
        )

    _SOURCE_CLIENT_CACHE[key] = client
    return client
