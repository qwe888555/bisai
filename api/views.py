"""API 视图层（备用）.

目前所有端点已通过 api.py 中的 Ninja Router 定义。
此文件可用于：
1. 基于函数的视图（FBV）或基于类的视图（CBV）
2. 文件上传/下载处理
3. 异步视图示例
"""
# 异步视图示例 — 当需要非阻塞 I/O 时可使用 async def：
#
# from django.http import JsonResponse
# from asgiref.sync import sync_to_async
# from api.services import get_item
#
# async def async_item_detail(request, item_id: int):
#     """异步获取商品详情（适合 I/O 密集型场景）. """
#     item = await sync_to_async(get_item)(item_id)
#     return JsonResponse({"id": item.id, "title": item.title})
