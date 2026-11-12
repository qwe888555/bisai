"""face 服务的请求/响应模型。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    # 两张图都必须是 data URI（见 engine.py 契约）
    referenceImage: str = Field(..., min_length=30, description="报名档案照片 data URI")
    captureImage: str = Field(..., min_length=30, description="现场采集照片 data URI")
    threshold: float = Field(
        0.30, gt=0.0, le=1.0, description="Facenet512 cosine 距离阈值，distance<=threshold 判一致"
    )


class VerifyResponse(BaseModel):
    machineStatus: Literal["MATCH", "MISMATCH", "UNAVAILABLE"]
    machineVerified: bool
    distance: Optional[float] = None
    threshold: float
    referenceIsReal: Optional[bool] = None
    captureIsReal: Optional[bool] = None
    reason: str


class HealthResponse(BaseModel):
    status: str
    model: str
    detector: str
    weightsReady: bool
