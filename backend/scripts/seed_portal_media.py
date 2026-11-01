#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补齐门户资源的演示内容：封面图、轮播视频、文字介绍与合作伙伴。

- 首页轮播：给「实时直播」补一段可循环播放的足球视频与说明；
- 新闻配图：把占位渐变图换成足球实拍封面，并补文字介绍；
- 合作伙伴：生成机构徽标作为封面，补机构简介，并补齐缺省的合作单位。

封面照片来自 Pexels（免费授权）的公开示例素材，下载后缓存在
``data/demo_assets/``；离线时会退回本地生成的渐变封面，脚本仍可正常执行。
正式环境请在管理端「门户资源」里替换成自己的图片。

用法（backend/ 目录下）：
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python scripts/seed_portal_media.py

脚本幂等：只更新缺失字段，可重复执行。
"""

from __future__ import annotations

import io
import os
import sys
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from apps.accounts.models import Account  # noqa: E402
from apps.attachments.models import FileRecord  # noqa: E402
from apps.attachments.services import upload  # noqa: E402
from apps.common.ids import new_id  # noqa: E402
from apps.events.models import Content, PortalResource  # noqa: E402

DEMO_VIDEO = "https://videos.pexels.com/video-files/5235804/5235804-hd_1920_1080_25fps.mp4"
CACHE_DIR = BACKEND_DIR / "data" / "demo_assets"

ADMIN = (
    Account.objects.filter(pk="acc_admin").first()
    or Account.objects.filter(username="admin").first()
)

CAROUSEL_SPECS = [
    ("实时直播", "全省赛场直播入口：实时画面、比分与图文直播同步更新。", DEMO_VIDEO),
]

# 新闻配图：标题 → (Pexels 图片编号, 离线渐变备用配色, 关联内容标题)
# 关联内容决定这张图展示在哪条赛事资讯上，管理端修改后立即同步到门户。
NEWS_IMAGE_SPECS = [
    ("赛事资讯", "赛场内外的最新动态，赛程、战报与球队故事集中呈现。",
     "274422", ((18, 64, 52), (44, 140, 100)), "决赛集锦：示例第一中学 2:1 示例第二中学"),
    ("四川省青少年校园足球联赛", "2026 赛季全省中小学足球联赛主视觉。",
     "270085", ((15, 52, 96), (36, 108, 168)), "2026年四川省青少年校园足球联赛报名启动"),
    ("小组赛精彩收官", "小组赛阶段收官，各组出线名额尘埃落定。",
     "918798", ((46, 30, 88), (108, 74, 168)), "四川省青少年校园足球联赛小组赛精彩收官"),
    ("校园足球特色学校", "全省校园足球特色学校巡礼，展示基层足球育人成果。",
     "114296", ((76, 40, 14), (184, 104, 36)), "校园足球特色学校名单公布"),
]

# 其余已发布内容的配图池（Unsplash 免费授权素材），按顺序分配，避免封面重复。
PHOTO_POOL = [
    ("unsplash", "1517466787929-bc90951d0974"),
    ("unsplash", "1522778119026-d647f0596c20"),
    ("unsplash", "1526232761682-d26e03ac148e"),
    ("unsplash", "1551958219-acbc608c6377"),
    ("unsplash", "1560272564-c83b66b1ad12"),
    ("unsplash", "1593341646782-e0b495cff86d"),
    ("unsplash", "1543326727-cf6c39e8f84c"),
    ("unsplash", "1518604666860-9ed391f76460"),
    ("unsplash", "1574629810360-7efbbe195018"),
    ("unsplash", "1606925797300-0b35e9d1794e"),
    ("unsplash", "1553778263-73a83bab9b0c"),
    ("unsplash", "1579952363873-27f3bade9f55"),
    ("unsplash", "1459865264687-595d652de67e"),
    ("unsplash", "1431324155629-1a6deb1dec8d"),
]

# 合作伙伴：标题 → (徽标简称, 配色)
PARTNER_SPECS = [
    ("四川省校园足球协会", "统筹全省校园足球竞赛组织、教练培训与青训体系建设。",
     "校园足协", ((8, 58, 44), (28, 122, 84)), 1),
    ("四川省学生体育艺术协会", "组织学生体育艺术活动，推动校园足球与美育协同发展。",
     "艺体协会", ((10, 42, 88), (42, 116, 176)), 2),
    ("四川省足球协会", "指导青少年足球竞赛规则与裁判员队伍建设。",
     "足球协会", ((76, 40, 14), (184, 104, 36)), 3),
]

COVER_PREFIX = "demo-cover-"
LOGO_PREFIX = "demo-partner-"


# ---------------------------------------------------------------- 图片处理


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 - 继续尝试下一个字体
                continue
    return ImageFont.load_default()


def _gradient(size: tuple[int, int], colors) -> "object":
    from PIL import Image, ImageDraw

    start, end = colors
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3)),
        )
    return image


def _fetch_photo(source: str, photo_id: str) -> bytes | None:
    """读取缓存的示例照片，没有则从 Pexels / Unsplash 公共 CDN 下载。"""
    cached = CACHE_DIR / f"{source}-{photo_id}.jpg"
    if cached.exists():
        return cached.read_bytes()
    legacy = CACHE_DIR / f"px-{photo_id}.jpg" if source == "pexels" else None
    if legacy is not None and legacy.exists():
        return legacy.read_bytes()
    if source == "unsplash":
        url = f"https://images.unsplash.com/photo-{photo_id}?w=1600&q=80&fm=jpg"
    else:
        url = (
            f"https://images.pexels.com/photos/{photo_id}/pexels-photo-{photo_id}.jpeg"
            "?auto=compress&cs=tinysrgb&w=1600"
        )
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        if not data.startswith(b"\xff\xd8"):
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data
    except Exception:  # noqa: BLE001 - 离线时退回渐变封面
        return None


def _photo_cover(data: bytes) -> bytes:
    """裁成 16:9 并压缩成适合卡片展示的 JPEG。"""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        target = 16 / 9
        if width / height > target:
            crop_width = int(height * target)
            left = (width - crop_width) // 2
            image = image.crop((left, 0, left + crop_width, height))
        else:
            crop_height = int(width / target)
            top = (height - crop_height) // 2
            image = image.crop((0, top, width, top + crop_height))
        image = image.resize((1280, 720), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
        return buffer.getvalue()


def _gradient_cover(title: str, colors) -> bytes:
    """离线备用封面：沿用管理端占位图的球场线条风格。"""
    from PIL import ImageDraw

    image = _gradient((1280, 720), colors)
    draw = ImageDraw.Draw(image)
    for x in range(-720, 1280 + 720, 110):
        draw.line([(x, 0), (x + 720, 720)], fill=(255, 255, 255), width=2)
    draw.ellipse([800, -250, 1344, 518], outline=(255, 255, 255), width=4)
    draw.text((90, 300), title[:16], font=_font(64, bold=True), fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _partner_logo(label: str, colors) -> bytes:
    """生成机构徽标：渐变底 + 足球纹样 + 简称。"""
    import math

    from PIL import Image, ImageDraw

    size = 640
    image = _gradient((size, size), colors)
    draw = ImageDraw.Draw(image)
    for x in range(-size, size * 2, 120):
        draw.line([(x, 0), (x + size, size)], fill=(255, 255, 255), width=1)

    # 管理端卡片按 2:1 裁切方形图，球标与简称要落在竖直居中区域才不会被裁掉。
    cx, cy, ball_r = 186, 300, 106
    draw.ellipse([cx - ball_r, cy - ball_r, cx + ball_r, cy + ball_r], fill=(255, 255, 255))
    # 中央五边形 + 向外辐射的球面接缝
    inner_r = 44
    inner = [
        (cx + inner_r * math.sin(math.radians(90 + i * 72)),
         cy - inner_r * math.cos(math.radians(90 + i * 72)))
        for i in range(5)
    ]
    draw.polygon(inner, fill=(11, 45, 34))
    for x, y in inner:
        angle = math.atan2(y - cy, x - cx)
        draw.line(
            [(x, y),
             (cx + (ball_r - 3) * math.cos(angle), cy + (ball_r - 3) * math.sin(angle))],
            fill=(11, 45, 34),
            width=5,
        )

    draw.text((330, cy - 44), label, font=_font(62, bold=True), fill=(255, 255, 255))
    draw.text((332, cy + 34), "青少年校园足球", font=_font(26), fill=(210, 255, 120))
    draw.text((28, 596), "四川省青少年校园足球联赛", font=_font(22), fill=(255, 255, 255))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- 资源更新


def _ensure_admin() -> Account:
    if ADMIN is None:
        raise SystemExit("未找到管理员账号 acc_admin，请先执行 seed_demo")
    return ADMIN


def _existing_name(resource: PortalResource) -> str:
    record = FileRecord.objects.filter(pk=resource.image_file_id).first()
    return record.original_name if record else ""


def _attach_image(resource: PortalResource, name: str, data: bytes, content_type: str) -> None:
    record = upload(
        _ensure_admin(),
        "RESOURCE",
        resource.pk,
        "CMS_IMAGE",
        SimpleUploadedFile(name, data, content_type=content_type),
    )
    resource.image_file_id = record.pk
    resource.save(update_fields=["image_file_id", "updated_at"])


def seed_carousel() -> int:
    updated = 0
    for title, description, video_url in CAROUSEL_SPECS:
        resource = PortalResource.objects.filter(
            resource_type="CAROUSEL", title=title
        ).first()
        if resource is None:
            print(f"  ! 未找到轮播资源「{title}」，跳过（请在管理端先创建）")
            continue
        changed = []
        if not resource.description:
            resource.description = description
            changed.append("description")
        if not resource.video_url:
            resource.video_url = video_url
            changed.append("video_url")
        if changed:
            resource.save(update_fields=changed + ["updated_at"])
            updated += 1
            print(f"  + 轮播「{title}」补充：{'、'.join(changed)}")
    return updated


def seed_news_images() -> int:
    updated = 0
    for index, (title, description, photo_id, colors, target_title) in enumerate(
        NEWS_IMAGE_SPECS, start=1
    ):
        resource = PortalResource.objects.filter(
            resource_type="NEWS_LINK_IMAGE", title=title
        ).first()
        if resource is None:
            print(f"  ! 未找到新闻配图「{title}」，跳过")
            continue

        if not resource.description:
            resource.description = description
            resource.save(update_fields=["description", "updated_at"])
            updated += 1
            print(f"  + 新闻配图「{title}」补充文字介绍")

        # 绑定到指定内容：门户赛事资讯页按 targetContentId 取这张图做封面。
        target = Content.objects.filter(title=target_title).first()
        if target is not None and resource.target_content_id != target.pk:
            resource.target_content_id = target.pk
            resource.save(update_fields=["target_content_id", "updated_at"])
            updated += 1
            print(f"  + 新闻配图「{title}」关联内容「{target_title}」")

        if _existing_name(resource).startswith(COVER_PREFIX):
            continue
        photo = _fetch_photo("pexels", photo_id)
        if photo:
            try:
                data = _photo_cover(photo)
                name = f"{COVER_PREFIX}{index}.jpg"
                content_type = "image/jpeg"
                source = "实拍封面"
            except Exception:  # noqa: BLE001 - 解码失败退回渐变
                photo = None
        if not photo:
            data = _gradient_cover(title, colors)
            name = f"{COVER_PREFIX}{index}.png"
            content_type = "image/png"
            source = "渐变封面（离线）"
        _attach_image(resource, name, data, content_type)
        updated += 1
        print(f"  + 新闻配图「{title}」更换{source}")
    return updated


def seed_content_covers() -> int:
    """为每条已发布内容补一条「新闻配图」，让管理端条目与赛事资讯一一对应。"""
    covered = set(
        PortalResource.objects.filter(resource_type="NEWS_LINK_IMAGE")
        .exclude(target_content_id="")
        .values_list("target_content_id", flat=True)
    )
    contents = (
        Content.objects.filter(status="PUBLISHED")
        .exclude(pk__in=covered)
        .order_by("-published_at")
    )
    created = 0
    fallback_colors = [
        ((18, 64, 52), (44, 140, 100)),
        ((15, 52, 96), (36, 108, 168)),
        ((46, 30, 88), (108, 74, 168)),
        ((76, 40, 14), (184, 104, 36)),
    ]
    for index, content in enumerate(contents):
        resource = PortalResource.objects.create(
            id=new_id("pres"),
            resource_type="NEWS_LINK_IMAGE",
            title=content.title[:150],
            description=(content.summary or "")[:300],
            target_content_id=content.pk,
            sort_order=20 + index,
            enabled=True,
        )
        source, photo_id = PHOTO_POOL[index % len(PHOTO_POOL)]
        photo = _fetch_photo(source, photo_id)
        data = name = content_type = ""
        if photo:
            try:
                data = _photo_cover(photo)
                name = f"{COVER_PREFIX}auto-{index + 1}.jpg"
                content_type = "image/jpeg"
            except Exception:  # noqa: BLE001 - 解码失败退回渐变
                data = ""
        if not data:
            data = _gradient_cover(content.title, fallback_colors[index % len(fallback_colors)])
            name = f"{COVER_PREFIX}auto-{index + 1}.png"
            content_type = "image/png"
        _attach_image(resource, name, data, content_type)
        created += 1
        print(f"  + 新闻配图「{content.title[:24]}」已创建并关联内容")
    return created


def seed_partners() -> tuple[int, int]:
    created = updated = 0
    for title, description, label, colors, sort_order in PARTNER_SPECS:
        resource = PortalResource.objects.filter(
            resource_type="PARTNER", title=title
        ).first()
        if resource is None:
            resource = PortalResource.objects.create(
                id=new_id("pres"),
                resource_type="PARTNER",
                title=title,
                description=description,
                sort_order=sort_order,
                enabled=True,
            )
            created += 1
            print(f"  + 新增合作伙伴「{title}」")
        elif not resource.description:
            resource.description = description
            resource.save(update_fields=["description", "updated_at"])
            updated += 1
            print(f"  + 合作伙伴「{title}」补充简介")

        if _existing_name(resource).startswith(LOGO_PREFIX):
            continue
        data = _partner_logo(label, colors)
        _attach_image(resource, f"{LOGO_PREFIX}{sort_order}.png", data, "image/png")
        updated += 1
        print(f"  + 合作伙伴「{title}」生成徽标封面")
    return created, updated


def main() -> None:
    print("轮播资源")
    updated = seed_carousel()
    print("新闻配图")
    updated += seed_news_images()
    news_created = seed_content_covers()
    print("合作伙伴")
    partner_created, partner_updated = seed_partners()
    updated += partner_updated
    created = news_created + partner_created

    cache.clear()
    print(f"完成：新增 {created} 条，更新 {updated} 条")
    print(
        f"轮播/新闻配图 "
        f"{PortalResource.objects.filter(resource_type__in=['CAROUSEL', 'NEWS_LINK_IMAGE']).count()} 条，"
        f"合作伙伴 {PortalResource.objects.filter(resource_type='PARTNER').count()} 条"
    )


if __name__ == "__main__":
    main()
