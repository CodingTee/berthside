"""SDOC engine package.

Layers:
    classifier  -- email categorisation (ordered rules, extensible)
    extractor   -- field extraction from SI/BL attachments (txt/pdf/xlsx/docx)
    comparator  -- normalisation + field-by-field comparison
    engine      -- orchestration: inbox -> results -> submission (+ review store)
"""
from .classifier import Classifier
from .comparator import FieldComparison, compare_fields
from .engine import Engine, EmailResult

__all__ = ["Classifier", "FieldComparison", "compare_fields", "Engine", "EmailResult"]
