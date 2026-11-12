"""DeepFace 1:1 人脸比对引擎（ONNX 后端，含防翻拍）。

硬契约（踩坑记录，勿改）：
1. 入参图片必须是 ``data:{mime};base64,<payload>`` 形式的 data URI，
   纯 base64 字符串会被 DeepFace 当成文件路径，报 422 且错误信息不指向真实原因。
2. 防翻拍必须走 ``DeepFace.extract_faces(anti_spoofing=True)`` 取
   ``faces[0]["is_real"]``；``verify(anti_spoofing=True)`` 的返回值里没有该字段，
   该参数等于无效且不报错。
3. ``extract_faces`` 不接受 ``silent`` 参数，传了直接 TypeError。
4. 相似度阈值由服务端按 ``distance <= threshold`` 自判，不依赖版本内置阈值。
5. 任何无法判定的情况（服务异常、无脸、参考照缺失）一律 UNAVAILABLE，
   绝不编造 MATCH。
"""

from __future__ import annotations

import base64
import re
from typing import Any

MODEL_NAME = "Facenet512"
DETECTOR_BACKEND = "retinaface"
DISTANCE_METRIC = "cosine"

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)

_WEIGHT_FILES = (
    "facenet512_weights.onnx",
    "retinaface.onnx",
    "2.7_80x80_MiniFASNetV2.onnx",
    "4_0_0_80x80_MiniFASNetV1SE.onnx",
)


def decode_data_uri(uri: str) -> tuple[bytes, str]:
    """解析 data URI，返回 (原始字节, MIME)。非法输入抛 ValueError。"""
    match = _DATA_URI_RE.match((uri or "").strip())
    if match is None:
        raise ValueError("图片必须是 data:{mime};base64,<payload> 格式")
    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("base64 载荷不合法") from exc
    if not raw:
        raise ValueError("图片字节为空")
    return raw, match.group("mime")


def _decode_image(uri: str):
    import cv2
    import numpy as np

    img_bytes, _ = decode_data_uri(uri)
    image = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("图片解码失败，请确认是 JPEG/PNG 真图")
    return image


def _is_real(image: Any) -> tuple[bool | None, str]:
    """返回 (是否真实人脸, 备注)。检测不到脸时 (None, 原因)。"""
    from deepface import DeepFace

    # 注意：extract_faces 不接受 silent 参数。
    faces = DeepFace.extract_faces(
        img_path=image,
        detector_backend=DETECTOR_BACKEND,
        enforce_detection=True,
        anti_spoofing=True,
        align=True,
    )
    if not faces:
        return None, "未检测到人脸"
    return bool(faces[0].get("is_real", False)), ""


def _base_result(threshold: float) -> dict[str, Any]:
    return {
        "machineStatus": "UNAVAILABLE",
        "machineVerified": False,
        "distance": None,
        "threshold": float(threshold),
        "referenceIsReal": None,
        "captureIsReal": None,
        "reason": "",
    }


def verify(reference_image: str, capture_image: str, threshold: float) -> dict[str, Any]:
    """执行 1:1 比对，返回字段与 VerifyResponse 对齐。"""
    result = _base_result(threshold)

    try:
        reference = _decode_image(reference_image)
        capture = _decode_image(capture_image)
    except ValueError as exc:
        result["reason"] = f"入参不可用：{exc}"
        return result

    try:
        ref_real, ref_note = _is_real(reference)
        cap_real, cap_note = _is_real(capture)
    except Exception as exc:  # noqa: BLE001 - 模型/权重异常都算不可用
        result["reason"] = f"防翻拍检测失败：{type(exc).__name__}"
        return result

    result["referenceIsReal"] = ref_real
    result["captureIsReal"] = cap_real

    # 任一图被判定为翻拍/非真人 → 直接 MISMATCH，不再算距离
    if ref_real is False or cap_real is False:
        result.update(
            machineStatus="MISMATCH",
            reason="防翻拍判定：存在非真实人脸（疑似翻拍/屏幕攻击）",
        )
        return result

    if ref_real is None or cap_real is None:
        result["reason"] = ref_note or cap_note or "未检测到人脸，无法比对"
        return result

    try:
        from deepface import DeepFace

        outcome = DeepFace.verify(
            img1_path=reference,
            img2_path=capture,
            model_name=MODEL_NAME,
            detector_backend=DETECTOR_BACKEND,
            distance_metric=DISTANCE_METRIC,
            enforce_detection=True,
            align=True,
            silent=True,
        )
        distance = float(outcome["distance"])
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"人脸比对失败：{type(exc).__name__}"
        return result

    result["distance"] = round(distance, 6)
    matched = distance <= float(threshold)
    result.update(
        machineStatus="MATCH" if matched else "MISMATCH",
        machineVerified=matched,
        reason="同一人（距离不超过阈值）" if matched else "不是同一人（距离超过阈值）",
    )
    return result


def weights_ready() -> bool:
    """检查 4 份 ONNX 权重是否已预置到 ~/.deepface/weights。"""
    from pathlib import Path

    weights_dir = Path.home() / ".deepface" / "weights"
    return all((weights_dir / name).exists() for name in _WEIGHT_FILES)
