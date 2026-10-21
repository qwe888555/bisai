"""文件存储抽象：本地磁盘（默认）或 MinIO。

契约 5：私有文件不暴露存储直链，一律经 /files/{fileId}/content 授权读取；
本模块只负责把字节放好，授权判断在 services 层。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Optional

from apps.common import conf


class LocalStorage:
    """开发默认：写 STORAGE_LOCAL_ROOT 下的两级散列目录。"""

    backend = "local"

    def __init__(self) -> None:
        self.root = Path(conf.STORAGE_LOCAL_ROOT).expanduser()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key

    def save(self, key: str, stream: BinaryIO) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as target:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        return key

    def size(self, key: str) -> int:
        path = self._path(key)
        return path.stat().st_size if path.exists() else 0

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            os.remove(path)


class MinioStorage:
    """生产可选：桶内私有对象，只能由服务端凭密钥读取。"""

    backend = "minio"

    def __init__(self) -> None:
        from minio import Minio  # 仅在该后端启用时导入

        self.client = Minio(
            conf.MINIO_ENDPOINT,
            access_key=conf.MINIO_ACCESS_KEY,
            secret_key=conf.MINIO_SECRET_KEY,
            secure=conf.MINIO_SECURE,
        )
        self.bucket = conf.MINIO_BUCKET
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def save(self, key: str, stream: BinaryIO) -> str:
        import io

        data = stream.read()
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data))
        return key

    def size(self, key: str) -> int:
        try:
            return self.client.stat_object(self.bucket, key).size
        except Exception:  # noqa: BLE001
            return 0

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(self.bucket, key)

    def delete(self, key: str) -> None:
        try:
            self.client.remove_object(self.bucket, key)
        except Exception:  # noqa: BLE001
            pass


_storage: Optional[object] = None


def get_storage():
    """按配置返回存储实现；进程内复用同一实例。"""
    global _storage
    if _storage is None:
        if conf.STORAGE_BACKEND == "minio" and conf.MINIO_ENDPOINT:
            _storage = MinioStorage()
        else:
            _storage = LocalStorage()
    return _storage
