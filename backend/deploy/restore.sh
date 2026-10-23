#!/usr/bin/env bash
# 回滚：从备份目录恢复 MySQL + 上传文件 + MinIO
# 用法（在 backend/ 下执行）：bash deploy/restore.sh backups/20261023-101500
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="${1:?用法: bash deploy/restore.sh <备份目录>}"
DB_NAME="${DB_NAME:-football}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-football}"

[ -f "$SRC/db.sql.gz" ] || { echo "缺少 $SRC/db.sql.gz"; exit 1; }

echo "[1/3] 恢复数据库 $DB_NAME（覆盖现有数据）"
gunzip -c "$SRC/db.sql.gz" | docker compose exec -T db \
  mysql -uroot -p"$DB_ROOT_PASSWORD" --default-character-set=utf8mb4 "$DB_NAME"

echo "[2/3] 恢复上传文件"
if [ -f "$SRC/files.tar.gz" ]; then
  tar -xzf "$SRC/files.tar.gz" -C .
else
  echo "  （备份里没有 files.tar.gz，跳过）"
fi

echo "[3/3] 恢复 MinIO 数据卷"
if [ -f "$SRC/minio.tar.gz" ]; then
  docker run --rm \
    -v "$(basename "$PWD")_minio_data":/data \
    -v "$PWD/$SRC":/backup \
    alpine sh -c 'rm -rf /data/* && tar -xzf /backup/minio.tar.gz -C /data'
else
  echo "  （备份里没有 minio.tar.gz，跳过）"
fi

echo "恢复完成，请执行：docker compose restart web && curl -s http://127.0.0.1/api/v1/health"
