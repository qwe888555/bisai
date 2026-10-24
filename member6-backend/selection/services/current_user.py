"""Resolve the current user id for audit fields.

Standalone demo: read ``X-User-Id`` (default 0).
Merged project: point SELECTION_CURRENT_USER_PROVIDER at a callable that
takes the Django request and returns the logged-in user's id.
"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("selection")


def get_current_user_id(request):
    provider_path = getattr(settings, "SELECTION_CURRENT_USER_PROVIDER", "")
    if provider_path:
        try:
            provider = import_string(provider_path)
            value = provider(request)
            return int(value) if value is not None else 0
        except Exception:
            logger.exception("当前用户提供器执行失败: %s", provider_path)
            return 0

    mode = getattr(settings, "SELECTION_CURRENT_USER_MODE", "header")
    if mode == "header":
        raw = request.headers.get("X-User-Id", "0")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if mode == "django":
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return int(user.id)
        return 0

    return 0
