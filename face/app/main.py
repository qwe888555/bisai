"""face 服务入口：POST /verify（1:1 人脸比对）、GET /health。"""

from __future__ import annotations

from fastapi import FastAPI

from . import engine
from .schemas import HealthResponse, VerifyRequest, VerifyResponse

app = FastAPI(
    title="football-face",
    description="DeepFace 1:1 人脸比对（Facenet512 + retinaface + MiniFASNet 防翻拍）",
    version="1.0.0",
)


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> dict:
    return {
        "status": "ok",
        "model": engine.MODEL_NAME,
        "detector": engine.DETECTOR_BACKEND,
        "weightsReady": engine.weights_ready(),
    }


@app.post("/verify", response_model=VerifyResponse, tags=["verify"])
def verify_endpoint(payload: VerifyRequest) -> dict:
    return engine.verify(payload.referenceImage, payload.captureImage, payload.threshold)
