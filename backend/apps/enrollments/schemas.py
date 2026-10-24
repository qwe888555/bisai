"""报名域请求模型（全部继承 StrictSchema，未知字段直接 400）。

字段名严格按契约 camelCase；PUT 为整份替换语义。
"""

from __future__ import annotations

from typing import Literal

from ninja import Field

from apps.common.schemas import StrictSchema

Phone = Field(..., pattern=r"^1\d{10}$", max_length=11)


class EnrollmentCreateIn(StrictSchema):
    eventId: str = Field(..., min_length=1, max_length=64)
    groupId: str = Field(..., min_length=1, max_length=64)
    teamId: str = Field(..., min_length=1, max_length=64)
    subjectType: Literal["SCHOOL_TEAM", "UNIVERSITY", "CITY"]
    contactName: str = Field(..., min_length=1, max_length=100)
    contactPhone: str = Phone
    remarks: str = Field(default="", max_length=2000)


class EnrollmentUpdateIn(StrictSchema):
    eventId: str = Field(..., min_length=1, max_length=64)
    groupId: str = Field(..., min_length=1, max_length=64)
    teamId: str = Field(..., min_length=1, max_length=64)
    subjectType: Literal["SCHOOL_TEAM", "UNIVERSITY", "CITY"]
    contactName: str = Field(..., min_length=1, max_length=100)
    contactPhone: str = Phone
    remarks: str = Field(default="", max_length=2000)
    version: int = Field(..., ge=0)


class RosterItemCreateIn(StrictSchema):
    athleteId: str = Field(..., min_length=1, max_length=64)
    jerseyNumber: int | None = Field(default=None, ge=0, le=999)
    position: str = Field(default="", max_length=50)
    photoFileId: str = Field(default="", max_length=64)
    studentStatusFileIds: list[str] = Field(default_factory=list, max_length=10)


class RosterItemUpdateIn(StrictSchema):
    athleteId: str = Field(..., min_length=1, max_length=64)
    jerseyNumber: int | None = Field(default=None, ge=0, le=999)
    position: str = Field(default="", max_length=50)
    photoFileId: str = Field(default="", max_length=64)
    studentStatusFileIds: list[str] = Field(default_factory=list, max_length=10)
    version: int = Field(..., ge=0)


class VersionRequestIn(StrictSchema):
    version: int = Field(..., ge=0)


class ReasonVersionRequestIn(StrictSchema):
    version: int = Field(..., ge=0)
    reason: str = Field(..., min_length=1, max_length=500)


class ReviewDecisionIn(StrictSchema):
    version: int = Field(..., ge=0)
    # TODO(确认项#1): 契约只有 APPROVE/REJECT；规则第6条要求"退回补正"与"否决"分开，
    # 默认实现三态 RETURN_FOR_CORRECTION，需成员1扩展契约。
    decision: Literal["APPROVE", "REJECT", "RETURN_FOR_CORRECTION"]
    reason: str = Field(default="", max_length=500)


class EnrollmentAttachmentCreateIn(StrictSchema):
    materialType: Literal["STAMPED_FORM", "STUDENT_STATUS"]
    fileId: str = Field(..., min_length=1, max_length=64)
    athleteId: str = Field(default="", max_length=64)


class ImportOptionsIn(StrictSchema):
    mode: Literal["VALIDATE_ONLY", "APPLY_VALID_ROWS"]


class EnrollmentFiltersIn(StrictSchema):
    eventId: str = Field(..., min_length=1, max_length=64)
    groupId: str = Field(default="", max_length=64)
    cityOrgId: str = Field(default="", max_length=64)
    teamName: str = Field(default="", max_length=150)
    subjectType: Literal["SCHOOL_TEAM", "UNIVERSITY", "CITY"] | None = None
    status: Literal[
        "DRAFT",
        "PENDING_CITY_REVIEW",
        "PENDING_PROVINCE_REVIEW",
        "APPROVED",
        "REJECTED",
        "WITHDRAWN",
    ] | None = None


class EnrollmentExportIn(StrictSchema):
    filters: EnrollmentFiltersIn
    scope: Literal["FILTERED", "SELECTED"]
    enrollmentIds: list[str] = Field(default_factory=list, max_length=100)
