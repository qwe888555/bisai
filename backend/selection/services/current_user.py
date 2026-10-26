"""Resolve the current user id for audit fields.

Standalone demo (``SELECTION_CURRENT_USER_MODE=header``): read ``X-User-Id``.
Merged project (``auto``): prefer the account attached by the main project's
JWT auth (``request.auth.id`` / ``request.user.id``) and fall back to the
header only for local demos.

Audit columns are opaque strings in member6 because the main project uses
string account ids such as ``acc_xxx``.  Numeric demo ids are normalized to
strings (``99`` -> ``"99"``).
"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("selection")


def _account_id(request):
    """Return an authenticated main-project account id, if any."""
    for attribute in ("auth", "user"):
        account = getattr(request, attribute, None)
        if account is None:
            continue
        # django.contrib.auth.AnonymousUser has is_authenticated == False.
        if getattr(account, "is_authenticated", True) is False:
            continue
        account_id = getattr(account, "id", None)
        if account_id is not None and str(account_id).strip():
            return str(account_id).strip()
    return ""


def get_current_user_id(request):
    provider_path = getattr(settings, "SELECTION_CURRENT_USER_PROVIDER", "")
    if provider_path:
        try:
            provider = import_string(provider_path)
            value = provider(request)
            return "" if value is None else str(value)
        except Exception:
            logger.exception("当前用户提供器执行失败: %s", provider_path)
            return ""

    mode = getattr(settings, "SELECTION_CURRENT_USER_MODE", "auto")
    if mode in ("auto", "django", "main"):
        account_id = _account_id(request)
        if account_id:
            return account_id

    if mode in ("auto", "header"):
        raw = request.headers.get("X-User-Id", "")
        return str(raw).strip() or "0"

    return ""
