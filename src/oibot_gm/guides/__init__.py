"""The leveling-guide tool (design §5.55): a member unlocks the RestedXP guide issued to their own account — the import
string plus the BattleTag it was issued to, which is the key — and gets a companion addon, an offline route explorer and
readable text, for themselves alone; and may lock it to another Battle.net account of their own (reissue). Pure functions
over text; the callers are the web routes (web/api_guides.py) and, for the re-issue, a DM (discord_bot.guide_convert).
The game-specific tables (zone ids, continent rects, dungeon tags) are profile data: profiles/<version>/guides.yaml."""
from .build import (  # noqa: F401
    ADDON_ENV, BATTLETAG_RE, BUSY, FILES, OUT, TEXT_MAX, TOO_BIG, GuideError, GuideGame, addon_zip_path, build, clear, lock_for,
    member_dir, status, validate_battletag,
)
from .reissue import (  # noqa: F401
    CHANNEL_REFUSAL, CONVERT_FAILED, EMPTY_NEW_TAG, SAME_TAG, USAGE, ConvertRequest, convert_request, reissue, reissue_filename,
)
from .rxp_decode import decode, encode  # noqa: F401
