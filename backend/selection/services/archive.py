"""Adapter between this module and member 4's player-archive data.

The main project should provide an implementation and point
``SELECTION_ARCHIVE_CLIENT`` at a callable that returns it.  The demo
implementation below is only used when ``SELECTION_ARCHIVE_MODE=demo``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from django.conf import settings
from django.utils.module_loading import import_string

from ..common import BusinessError
from ..enums import GroupType


@dataclass(frozen=True)
class SelectionCandidate:
    candidate_id: str
    event_id: str
    name: str
    team_name: str
    group_type: str
    position: str
    id_card_masked: str = ""


class SelectionArchiveClient(ABC):
    """Minimal interface required by the scoring module."""

    @abstractmethod
    def get_candidate(
        self, candidate_id: str, event_id: Optional[str] = None
    ) -> Optional[SelectionCandidate]:
        """Return a candidate or None when the archive id does not exist.

        ``event_id`` is optional for the demo client and used by the main
        project adapter to disambiguate a person enrolled in several events.
        """

    def list_candidates(self, group_type: str) -> List[SelectionCandidate]:
        """Return candidates of one group; used by the demo seeder."""
        return []


class DemoSelectionArchiveClient(SelectionArchiveClient):
    """45 deterministic fake candidates: 15 per group, event_id=1."""

    NAMES = [
        "张伟", "王芳", "李娜", "刘洋", "陈静",
        "杨帆", "赵磊", "黄敏", "周涛", "吴杰",
        "徐强", "孙丽", "马超", "朱婷", "胡军",
        "郭鹏", "何雪", "高翔", "林峰", "罗丹",
        "郑浩", "梁爽", "谢宇", "宋佳", "唐亮",
        "韩梅", "冯晨", "邓超", "曹阳", "彭飞",
        "曾琳", "肖楠", "田甜", "董健", "潘悦",
        "袁野", "蒋欣", "蔡明", "余乐", "杜鹏",
        "叶青", "程龙", "苏晴", "魏然", "吕航",
    ]

    GROUP_TEAMS = {
        GroupType.HIGHER: ["江城大学", "南湖大学", "东林大学"],
        GroupType.MIDDLE: ["江城一中", "南湖中学", "东林中学"],
        GroupType.PRIMARY: ["江城实验小学", "南湖小学", "东林小学"],
    }

    POSITIONS = ["前锋", "中场", "后卫", "门将"]

    def __init__(self):
        self._candidates: Dict[str, SelectionCandidate] = {}
        self._build()

    def _build(self):
        groups = [
            (GroupType.HIGHER, 10000, 0),
            (GroupType.MIDDLE, 20000, 15),
            (GroupType.PRIMARY, 30000, 30),
        ]
        for group_type, base_id, name_offset in groups:
            teams = self.GROUP_TEAMS[group_type]
            for index in range(15):
                numeric_id = base_id + index + 1
                candidate_id = str(numeric_id)
                self._candidates[candidate_id] = SelectionCandidate(
                    candidate_id=candidate_id,
                    event_id="1",
                    name=self.NAMES[name_offset + index],
                    team_name=teams[index % len(teams)],
                    group_type=group_type,
                    position=self.POSITIONS[index % len(self.POSITIONS)],
                    id_card_masked=(
                        f"{numeric_id}**********{numeric_id % 100:02d}"
                    ),
                )

    def get_candidate(
        self, candidate_id: str, event_id: Optional[str] = None
    ) -> Optional[SelectionCandidate]:
        return self._candidates.get(str(candidate_id))

    def list_candidates(self, group_type: str) -> List[SelectionCandidate]:
        return [
            candidate
            for candidate in self._candidates.values()
            if candidate.group_type == group_type
        ]


_CLIENT_CACHE = {}


def get_archive_client() -> SelectionArchiveClient:
    """Resolve the configured archive client, with a small instance cache.

    Modes:
    * ``auto``  – use the main project's member4 tables when this app is
                  installed next to them; otherwise fall back to demo data;
    * ``demo``  – always use the 45 fake candidates;
    * ``main``  – require the main project's tables;
    * custom    – ``SELECTION_ARCHIVE_CLIENT`` dotted factory wins.
    """
    mode = getattr(settings, "SELECTION_ARCHIVE_MODE", "auto")
    path = getattr(settings, "SELECTION_ARCHIVE_CLIENT", "")
    key = (mode, path)

    if key in _CLIENT_CACHE:
        return _CLIENT_CACHE[key]

    if path:
        try:
            factory = import_string(path)
            client = factory()
        except Exception as exc:
            raise BusinessError(
                500, f"档案客户端初始化失败，请检查 SELECTION_ARCHIVE_CLIENT: {exc}"
            )
        if not hasattr(client, "get_candidate"):
            raise BusinessError(
                500, "档案客户端必须实现 get_candidate(candidate_id)"
            )
    elif mode == "demo":
        client = DemoSelectionArchiveClient()
    elif mode in ("auto", "main"):
        from .main_adapters import main_data_available
        from .main_adapters import MainProjectArchiveClient

        if main_data_available():
            client = MainProjectArchiveClient()
        elif mode == "auto":
            client = DemoSelectionArchiveClient()
        else:
            raise BusinessError(
                500,
                "SELECTION_ARCHIVE_MODE=main，但主程序 apps.enrollments/"
                "apps.persons/apps.teams/apps.events 未安装",
            )
    else:
        raise BusinessError(
            500,
            "未配置球员档案客户端，请设置 SELECTION_ARCHIVE_MODE=auto/demo "
            "或提供 SELECTION_ARCHIVE_CLIENT",
        )

    _CLIENT_CACHE[key] = client
    return client
