"""Shared helpers for member6 tests."""

import os

from django.apps import apps


def api_prefix():
    """Return the URL prefix used by the running deployment.

    Standalone: ``/api/selection/``.
    Merged into the main backend: ``/api/v1/selection/``.

    ``SELECTION_TEST_API_PREFIX`` overrides both; pass it without a leading
    slash under Git Bash (``api/v1/selection/``) because MSYS rewrites
    leading-slash environment values into Windows paths.
    """
    prefix = os.environ.get("SELECTION_TEST_API_PREFIX")
    if not prefix:
        prefix = (
            "/api/v1/selection/"
            if apps.is_installed("apps.common")
            else "/api/selection/"
        )
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix
