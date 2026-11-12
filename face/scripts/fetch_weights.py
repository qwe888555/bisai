"""预置 DeepFace ONNX 权重到 ~/.deepface/weights（或指定目录）。

为什么需要它：DeepFace 首次调用时会自动下载权重，国内网络基本失败
（GitHub release 报 CERTIFICATE_VERIFY_FAILED、huggingface.co 不通）。
默认走 hf-mirror，可通过环境变量覆盖：

    WEIGHTS_BASE_URL   权重下载基址（默认 https://hf-mirror.com/serengil/deepface/resolve/main）
    DEEPFACE_WEIGHTS_DIR 目标目录（默认 ~/.deepface/weights）

Docker 构建时由 Dockerfile 调用本脚本，把权重烤进镜像，离线可用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WEIGHTS = (
    "facenet512_weights.onnx",
    "retinaface.onnx",
    "2.7_80x80_MiniFASNetV2.onnx",
    "4_0_0_80x80_MiniFASNetV1SE.onnx",
)


def _download(url: str, dest: Path) -> None:
    try:
        import requests

        with requests.get(url, stream=True, timeout=(15, 300)) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as target:
                for chunk in resp.iter_content(1 << 20):
                    if chunk:
                        target.write(chunk)
    except ImportError:
        import urllib.request

        urllib.request.urlretrieve(url, dest)


def main() -> int:
    base = os.environ.get(
        "WEIGHTS_BASE_URL",
        "https://hf-mirror.com/serengil/deepface/resolve/main",
    ).rstrip("/")
    target_dir = Path(
        os.environ.get(
            "DEEPFACE_WEIGHTS_DIR",
            str(Path.home() / ".deepface" / "weights"),
        )
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in WEIGHTS:
        dest = target_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[skip] {name} already present")
            continue
        url = f"{base}/{name}"
        print(f"[get] {url}")
        _download(url, dest)
        print(f"[ok]  {name} ({dest.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
