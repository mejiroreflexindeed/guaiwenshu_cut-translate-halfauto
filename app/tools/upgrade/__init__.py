"""v2 upgrade modules (plan 1.1, section 5).

Each module exposes the fixed Interface named in the plan; private helpers may change.
The legacy entry points under app/tools remain untouched and continue to work.
"""

from __future__ import annotations

PLAN_VERSION = "1.1"
SCHEMA_VERSION = 2

__all__ = ["PLAN_VERSION", "SCHEMA_VERSION"]
