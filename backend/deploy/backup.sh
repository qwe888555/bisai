#!/usr/bin/env bash
# 备份：MySQL dump + 上传文件 + MinIO 数据卷
# 用法（在 backend/ 下执行）：bash deploy/backup.sh [备份目录]
# 产物：backups/<时间戳>/{db.sql.gz, files.tar.gz, minio.tar.gz}
set -euo pipefail

cd "$(dirname "$0")/.."
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${1:-backups/$STAMP}"
mkdir -p "$OUT"

DB_NAME="${DB_NAME:-football}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-football}"

echo "[1/3] 导出数据库 $DB_NAME"
docker compose exec -T db \
  mysqldump -uroot -p"$DB_ROOT_PASSWORD" \
  --single-transaction --quick --routines --events \
  --default-character-set=utf8mb4 "$DB_NAME" | gzip > "$OUT/db.sql.gz"

echo "[2/3] 打包上传文件 backend/data/"
tar -czf "$OUT/files.tar.gz" -C . data 2>/dev/null || echo "  （无 data/ 目录，跳过）"

echo "[3/3] 打包 MinIO 数据卷"
docker run --rm \
  -v "$(basename "$PWD")_minio_data":/data \
  -v "$PWD/$OUT":/backup \
  alpine tar -czf /backup/minio.tar.gz -C /data .

echo "完成：$OUT"
ls -lh "$OUT"
