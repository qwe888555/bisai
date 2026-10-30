"""修复 FileRecord 有元数据但存储内容缺失的物理字节。

背景（联调报告 2026-10-30）：演示种子只写元数据不写字节，
file_d8bca28b0f7446009784（门户轮播图）等记录直链/缩略图 404
"文件内容缺失，请重新上传"。本命令按 storage_key 逐条检查，
缺失时按扩展名补最小合法占位字节（PDF/图片/Office），同步 size_bytes；
缩略图键缺字节时用主图重新生成。

用法：
    python manage.py repair_file_content                 # 全量扫描
    python manage.py repair_file_content --file-id file_xxx [--file-id ...]
    python manage.py repair_file_content --dry-run
"""

from __future__ import annotations

import io
import re

from django.core.management.base import BaseCommand

from apps.attachments.models import FileRecord


def _pdf_bytes(title: str) -> bytes:
    """最小可识别 PDF（浏览器可直接渲染）。"""
    text = re.sub(r"[^\x20-\x7e]", "", title) or "Demo material"
    stream = f"BT /F1 20 Tf 72 760 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(data)
    data += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    data += b"0000000000 65535 f \n"
    for offset in offsets:
        data += f"{offset:010d} 00000 n \n".encode("ascii")
    data += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(data)


def _image_bytes(ext: str) -> bytes:
    """生成占位图（球场线框风格），png/jpg 皆可。"""
    from PIL import Image, ImageDraw

    width, height = 960, 540
    image = Image.new("RGB", (width, height), (23, 42, 76))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=(int(23 + 30 * ratio), int(42 + 60 * ratio), int(76 + 90 * ratio)),
        )
    draw.rectangle([12, 12, width - 12, height - 12], outline=(240, 244, 255), width=3)
    draw.ellipse(
        [width * 0.34, height * 0.18, width * 0.66, height * 0.82],
        outline=(240, 244, 255), width=3,
    )
    draw.line([(width // 2, 12), (width // 2, height - 12)], fill=(240, 244, 255), width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG" if ext == "png" else "JPEG", quality=86)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    """最小 OOXML zip（docx/xlsx 共用，扩展名校验只看 ZIP 魔数）。"""
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _placeholder_bytes(record: FileRecord) -> bytes:
    ext = (record.storage_key or "").rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return _pdf_bytes(record.original_name or record.pk)
    if ext in {"png", "jpg", "jpeg"}:
        return _image_bytes(ext)
    if ext in {"docx", "xlsx"}:
        return _docx_bytes()
    if ext in {"doc", "xls"}:
        # OLE2 复合文档魔数 + 零填充，满足扩展名一致性校验
        return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    return (record.original_name or record.pk).encode("utf-8")


def _thumbnail_bytes(data: bytes, ext: str) -> bytes | None:
    from apps.attachments.services import _make_thumbnail

    thumb = _make_thumbnail(data)
    if thumb:
        return thumb
    if ext in {"png", "jpg", "jpeg"}:
        return _image_bytes("jpg")
    return None


class Command(BaseCommand):
    help = "修复 FileRecord 存储内容缺失（占位字节回填）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--file-id",
            action="append",
            dest="file_ids",
            default=[],
            help="只修指定 fileId，可重复；不传则全量扫描",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只列出将修复的记录，不写存储",
        )

    def handle(self, *args, **options):
        from apps.attachments.storage import get_storage

        records = FileRecord.objects.all()
        file_ids = options.get("file_ids") or []
        if file_ids:
            records = records.filter(pk__in=file_ids)
        storage = get_storage()
        dry_run = options["dry_run"]
        repaired = 0
        untouched = 0

        for record in records.iterator():
            changed = False
            data = None
            if storage.size(record.storage_key) == 0:
                data = _placeholder_bytes(record)
                changed = True
                if not dry_run:
                    storage.save(record.storage_key, io.BytesIO(data))
                    record.size_bytes = len(data)
                self.stdout.write(f"  补主文件字节 {record.pk} <- {record.storage_key}")
            if record.thumbnail_key and storage.size(record.thumbnail_key) == 0:
                source = data
                if source is None:
                    try:
                        source = storage.open(record.storage_key).read()
                    except FileNotFoundError:
                        source = None
                thumb = _thumbnail_bytes(
                    source or _image_bytes("jpg"), record.storage_key.rsplit(".", 1)[-1].lower()
                )
                if thumb:
                    changed = True
                    if not dry_run:
                        storage.save(record.thumbnail_key, io.BytesIO(thumb))
                    self.stdout.write(
                        f"  补缩略图字节 {record.pk} <- {record.thumbnail_key}"
                    )
            if changed:
                repaired += 1
                if not dry_run:
                    record.save()
            else:
                untouched += 1

        verb = "将修复" if dry_run else "已修复"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {repaired} 条，内容完好 {untouched} 条"
                + ("（dry-run，未写存储）" if dry_run else "")
            )
        )
