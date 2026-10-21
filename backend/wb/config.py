"""Data directory layout (spec §4.3).

~/WorkbenchData/
  profiles/{test,prod}/market/{CN,JP}/...   market data, per profile
  profiles/{test,prod}/app.sqlite           snapshots, capabilities, fetch log
  shared.sqlite                             rules, labels, notes, settings (both profiles)
  backups/                                  daily copies of shared.sqlite
Override the root with WB_DATA_DIR and the profile with WB_PROFILE.
"""
import os
from dataclasses import dataclass
from pathlib import Path

PROFILES = ("test", "prod")


@dataclass(frozen=True)
class Paths:
    root: Path
    profile: str

    @property
    def profile_dir(self) -> Path:
        return self.root / "profiles" / self.profile

    def market_dir(self, market: str) -> Path:
        return self.profile_dir / "market" / market

    @property
    def app_db(self) -> Path:
        return self.profile_dir / "app.sqlite"

    @property
    def shared_db(self) -> Path:
        return self.root / "shared.sqlite"

    @property
    def backup_dir(self) -> Path:
        return self.root / "backups"


def get_paths(profile: str | None = None, root: str | Path | None = None) -> Paths:
    profile = profile or os.environ.get("WB_PROFILE", "test")
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}, expected one of {PROFILES}")
    base = Path(root or os.environ.get("WB_DATA_DIR") or Path.home() / "WorkbenchData")
    return Paths(base.expanduser(), profile)
