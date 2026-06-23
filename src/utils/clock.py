"""Naive-UTC timestamp helper.

datetime.utcnow() is deprecated since Python 3.12 and scheduled for
removal, but every timestamp comparison and DB column default in this
codebase assumes a naive datetime in UTC (no tzinfo) — switching to
datetime.now(timezone.utc) directly would make those values
timezone-aware and raise TypeError when compared against the naive
datetimes already used throughout (e.g. caller-supplied `current_time`
parameters in tests and main.py). This preserves the exact naive-UTC
value utcnow() used to return, via the officially documented replacement.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
