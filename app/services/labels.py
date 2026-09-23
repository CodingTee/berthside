"""Turn a machine-written key into the printed label the extractor matches.

XML element names, JSON object keys and spreadsheet headers cannot contain
spaces, so a shipping-document schema has to spell its labels ``PortOfLoading``,
``port_of_loading`` or ``port-of-loading``. The extractor's label rules are
written against how those labels are *printed* ("Port of Loading"), so without
this every field silently reads as null and the document looks empty rather
than mislabelled.

Only word boundaries are inserted; nothing is renamed, translated or guessed,
so a key that already matches (``shipper``) passes through unchanged.

Shared by the XML reader and the JSON reader on purpose. Two copies of the rule
would drift, and the failure mode of a drifted copy is silent: the fields just
stop being found.
"""
from __future__ import annotations

import re

# camelCase / PascalCase boundaries. The second rule stops a run of capitals
# from being split into single letters ("SHPNumber" -> "SHP Number", not
# "S H P Number").
_CAMEL_LOWER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

# Separators a machine-written key may use in place of a space. A dot is
# included because flattened JSON often nests with one.
_SEPARATORS = re.compile(r"[_\-.]+")

# Anything left that is not a letter, digit or space is dropped rather than
# turned into a space, so "gross_weight_(kg)" keeps the parenthesis close to
# the unit the label rules expect: "gross weight (kg)".
_RUNS = re.compile(r"\s+")


def label_from_key(key: str) -> str:
    """``PortOfLoading`` / ``port_of_loading`` -> ``Port Of Loading``."""
    if not key:
        return ""
    spaced = _CAMEL_ACRONYM.sub(" ", _CAMEL_LOWER.sub(" ", str(key)))
    spaced = _SEPARATORS.sub(" ", spaced)
    spaced = "".join(ch if (ch.isalnum() or ch.isspace() or ch in "()/%&") else " "
                     for ch in spaced)
    return _RUNS.sub(" ", spaced).strip()
