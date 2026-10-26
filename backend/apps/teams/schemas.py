"""球队域只读响应投影（供门户/管理端补全球队、学校与市州名称）。

写入接口仍由成员4 的球队档案模块维护；本文件只做展示字段映射，
不新增字段、不改变成员4 的写链契约。
"""

from __future__ import annotations


def team_out(team) -> dict:
    return {
        "id": str(team.pk),
        "teamNo": team.team_no,
        "name": team.name,
        "category": team.category_code,
        "schoolName": team.school.name if team.school_id else None,
        "cityName": team.region.name if team.region_id else None,
        "intro": team.introduction,
        "coachName": getattr(team, "head_coach_name", None),
        "memberCount": getattr(team, "member_count", 0),
        "status": team.audit_status,
        "version": int(team.updated_at.timestamp() * 1_000_000),
        "createdAt": team.created_at.isoformat(),
    }
