"""The leveling-guide tool (design §5.55): a member unlocks the RestedXP guide issued to their own account — the import
string plus the BattleTag it was issued to, which is the key — and gets a companion addon, an offline route explorer and
readable text, for themselves alone. Pure functions over text; the web route (web/api_guides.py) is the only caller.
The game-specific tables (zone ids, continent rects, dungeon tags) are profile data: profiles/<version>/guides.yaml."""
from .build import (  # noqa: F401
    ADDON_ENV, BATTLETAG_RE, BUSY, FILES, OUT, TEXT_MAX, GuideError, GuideGame, addon_zip_path, build, clear, lock_for,
    member_dir, status, validate_battletag,
)
from .rxp_decode import decode, encode  # noqa: F401
