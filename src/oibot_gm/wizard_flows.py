"""Importing this registers every wizard flow in wizard.FLOWS (one module per area, so each stays small)."""
from . import flows_absence  # noqa: F401
from . import flows_config  # noqa: F401
from . import flows_misc  # noqa: F401
from . import flows_raid  # noqa: F401
from . import flows_register  # noqa: F401
from . import flows_roster  # noqa: F401
from .wizard import FLOWS  # noqa: F401

__all__ = ["FLOWS"]
