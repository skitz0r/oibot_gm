"""Importing this registers every wizard flow in wizard.FLOWS (one module per area, so each stays small)."""
from . import flows_absence  # noqa: F401
from .wizard import FLOWS  # noqa: F401

__all__ = ["FLOWS"]
