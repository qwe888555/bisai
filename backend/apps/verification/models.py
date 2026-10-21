"""身份核验模型。

契约 4.5：机器结果与人工结果**必须分开**。
人工确认只能改 manualStatus，永远不能把 manualStatus=CONFIRMED
改写成 machineVerified=true。
"""

from __future__ import annotations

from django.db import models

MACHINE_STATUS = ("NOT_RUN", "MATCH", "MISMATCH", "UNAVAILABLE")
MANUAL_STATUS = ("NOT_REVIEWED", "PENDING", "CONFIRMED", "REJECTED")
MODES = ("FACE_COMPARE", "MANUAL")


class IdentityCheck(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    match_id = models.CharField(max_length=64, db_index=True)
    enrollment_id = models.CharField(max_length=64, db_index=True)
    athlete_id = models.CharField(max_length=64, db_index=True)
    captured_photo_file_id = models.CharField(max_length=64)
    mode = models.CharField(max_length=20, choices=[(m, m) for m in MODES])
    machine_status = models.CharField(max_length=20, default="NOT_RUN")
    machine_verified = models.BooleanField(default=False)
    manual_status = models.CharField(max_length=20, default="NOT_REVIEWED")
    manual_reason = models.CharField(max_length=1000, blank=True, default="")
    reviewer_account_id = models.CharField(max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "verification_identity_check"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.athlete_id}@{self.match_id}"
