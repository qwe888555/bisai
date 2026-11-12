"""face 服务契约测试：不依赖真实权重，覆盖 data URI 解析与各判定分支。"""

from __future__ import annotations

import base64
import sys
import types

import pytest

from app import engine


def test_decode_data_uri_roundtrip():
    payload = b"hello-image-bytes"
    uri = "data:image/jpeg;base64," + base64.b64encode(payload).decode()
    raw, mime = engine.decode_data_uri(uri)
    assert raw == payload
    assert mime == "image/jpeg"


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-uri", "base64,abc", "data:image/png;base64,!!!", "data:;base64,AAAA"],
)
def test_decode_data_uri_rejects_bad(bad):
    with pytest.raises(ValueError):
        engine.decode_data_uri(bad)


def test_verify_bad_input_returns_unavailable():
    out = engine.verify("garbage", "garbage", 0.3)
    assert out["machineStatus"] == "UNAVAILABLE"
    assert out["machineVerified"] is False


def test_verify_spoof_path(monkeypatch):
    monkeypatch.setattr(engine, "_decode_image", lambda uri: object())
    monkeypatch.setattr(engine, "_is_real", lambda img: (False, ""))
    out = engine.verify(
        "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,AAAA", 0.3
    )
    assert out["machineStatus"] == "MISMATCH"
    assert "防翻拍" in out["reason"]


def test_verify_no_face_path(monkeypatch):
    monkeypatch.setattr(engine, "_decode_image", lambda uri: object())
    monkeypatch.setattr(engine, "_is_real", lambda img: (None, "未检测到人脸"))
    out = engine.verify(
        "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,AAAA", 0.3
    )
    assert out["machineStatus"] == "UNAVAILABLE"
    assert "未检测到人脸" in out["reason"]


def test_verify_match_path(monkeypatch):
    monkeypatch.setattr(engine, "_decode_image", lambda uri: object())
    monkeypatch.setattr(engine, "_is_real", lambda img: (True, ""))

    class _DeepFace:
        @staticmethod
        def verify(**kwargs):
            return {"distance": 0.12}

    monkeypatch.setitem(sys.modules, "deepface", types.SimpleNamespace(DeepFace=_DeepFace))
    out = engine.verify(
        "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,AAAA", 0.3
    )
    assert out["machineStatus"] == "MATCH"
    assert out["machineVerified"] is True
    assert out["distance"] == 0.12


def test_verify_mismatch_path(monkeypatch):
    monkeypatch.setattr(engine, "_decode_image", lambda uri: object())
    monkeypatch.setattr(engine, "_is_real", lambda img: (True, ""))

    class _DeepFace:
        @staticmethod
        def verify(**kwargs):
            return {"distance": 0.9}

    monkeypatch.setitem(sys.modules, "deepface", types.SimpleNamespace(DeepFace=_DeepFace))
    out = engine.verify(
        "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,AAAA", 0.3
    )
    assert out["machineStatus"] == "MISMATCH"
    assert out["machineVerified"] is False
