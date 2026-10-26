"""Compatibility shim.

The HTTP layer now uses django-ninja in :mod:`selection.api`.  This module is
kept so existing imports of ``selection.views.api`` (or the app's old module
path) keep working after the framework migration.
"""

from .api import api

__all__ = ["api"]
