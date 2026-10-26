"""Authentication bridge for standalone and merged deployments.

* Standalone (``SELECTION_AUTH_MODE=auto`` and the main project is absent):
  no login is required; ``X-User-Id`` is used for audit fields.
* Merged into ``backend/`` (``auto``): delegate to
  ``apps.common.authentication.JWTAuth``.  Missing/invalid tokens produce the
  main project's ``UNAUTHENTICATED`` error envelope.
* ``none``: explicitly disable auth (used by unit tests).
* ``main``: require the main project's JWT auth; configuration errors surface
  immediately instead of silently falling back to anonymous access.
"""

import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("selection")

_MAIN_JWT_AUTH = None
_MAIN_JWT_IMPORT_ERROR = None


def _import_main_jwt_auth():
    global _MAIN_JWT_AUTH, _MAIN_JWT_IMPORT_ERROR
    if _MAIN_JWT_AUTH is not None or _MAIN_JWT_IMPORT_ERROR is not None:
        return _MAIN_JWT_AUTH
    try:
        from apps.common.authentication import JWTAuth
    except Exception as exc:  # pragma: no cover - depends on deployment layout
        _MAIN_JWT_IMPORT_ERROR = exc
        return None
    _MAIN_JWT_AUTH = JWTAuth()
    return _MAIN_JWT_AUTH


class OpenAccess:
    """Truthy stand-in for anonymous access.

    django-ninja treats a falsy auth result as ``401 Unauthorized``, so the
    standalone "no login required" mode must return a truthy value.  It carries
    no identity; :func:`get_current_user_id` falls back to ``X-User-Id``.
    """

    is_authenticated = False
    id = None


OPEN_ACCESS = OpenAccess()


class SelectionAuth:
    """django-ninja auth class shared by all protected member6 routes."""

    def __call__(self, request):
        mode = getattr(settings, "SELECTION_AUTH_MODE", "auto")

        if mode not in ("auto", "demo", "header", "none", "main"):
            raise ImproperlyConfigured(
                f"未知的 SELECTION_AUTH_MODE: {mode!r}"
            )

        if mode == "none":
            return OPEN_ACCESS

        if mode == "main":
            main_auth = _import_main_jwt_auth()
            if main_auth is None:
                raise ImproperlyConfigured(
                    "SELECTION_AUTH_MODE=main，但无法导入 "
                    "apps.common.authentication.JWTAuth"
                ) from _MAIN_JWT_IMPORT_ERROR
            return main_auth(request)

        if mode == "auto":
            main_auth = _import_main_jwt_auth()
            if main_auth is not None:
                return main_auth(request)

        # Standalone/demo/header mode: authentication stays optional.  Audit
        # identity comes from X-User-Id (or SELECTION_CURRENT_USER_PROVIDER).
        return OPEN_ACCESS


def main_auth_available():
    return _import_main_jwt_auth() is not None
