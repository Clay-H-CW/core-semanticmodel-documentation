"""Shared DAX text utilities.

Split out from `ir.build` because both it and `bpa` need to scan raw DAX text for
identifier references — `ir.build` to resolve what a measure depends on, `bpa` to check
*how* a reference was written (qualified vs. not). Keeping both in `ir.build` would force
`bpa` to import from it, and `ir.build` calling into `bpa` at the end of `tmsl_to_model`
would make that a circular import.
"""

from __future__ import annotations

import re

# DAX reference patterns. Applied only after comments and string literals are stripped, so
# that text inside a quoted string cannot masquerade as an identifier.
QUALIFIED_REF = re.compile(r"'([^']+)'\[([^\]]+)\]|(\b\w+)\[([^\]]+)\]")
BARE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


def strip_dax_noise(expression: str) -> str:
    """Remove comments and string literals so reference matching cannot false-positive."""
    without_block = re.sub(r"/\*.*?\*/", " ", expression, flags=re.S)
    without_line = re.sub(r"(--|//)[^\n]*", " ", without_block)
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', without_line)
