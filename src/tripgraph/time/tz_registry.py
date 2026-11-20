"""Byte-pinned IANA timezone registry and exact PEP 495 DST resolver.

Contract (TripGraph v7):
- Direct ZoneInfo(key) calls are banned across src/tripgraph/.
- Every ZoneInfo object is constructed via ZoneInfo.from_file() from the exact
  raw TZif byte stream whose SHA-256 fingerprint is stored on TripGraph.
- Handles DST spring-forward gaps (exact binary-search transition) and
  DST fall-back overlaps (explicit fold_0 vs fold_1 selection).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
import zoneinfo

import tzdata

AmbiguousPolicy = Literal["fold_0", "fold_1"]
GapPolicy = Literal["shift_forward", "closed"]


@dataclass(frozen=True)
class PinnedTimezone:
    """A ZoneInfo instance bound cryptographically to its raw TZif bytes."""

    tz_iana: str
    tzinfo: zoneinfo.ZoneInfo
    source: str
    version_label: str
    tzif_sha256: str


@dataclass(frozen=True)
class ResolvedInstant:
    """Result of resolving a local wall-clock (date, time) to a UTC epoch second."""

    utc_epoch: int | None
    is_gap: bool
    is_ambiguous: bool
    resolution_note: str


class PinnedTzRegistry:
    """Loads, hashes, and caches TZif byte streams from explicit tzpath or tzdata."""

    def __init__(self, tzpath: tuple[str, ...] | None = None) -> None:
        self._tzpath: tuple[str, ...] = (
            tzpath if tzpath is not None else tuple(zoneinfo.TZPATH)
        )
        self._cache: dict[str, PinnedTimezone] = {}

    def get(self, tz_iana: str) -> PinnedTimezone:
        """Return a byte-pinned ZoneInfo object for an IANA key (e.g. 'Europe/Berlin')."""
        if tz_iana in self._cache:
            return self._cache[tz_iana]

        if not tz_iana or tz_iana.startswith("/") or ".." in tz_iana.split("/"):
            raise ValueError(f"Invalid IANA timezone key: {tz_iana!r}")

        raw_bytes, source, version_label = self._load_tzif_bytes(tz_iana)
        tzif_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        # Construct ZoneInfo EXCLUSIVELY from the exact bytes we just hashed.
        tzinfo = zoneinfo.ZoneInfo.from_file(io.BytesIO(raw_bytes), key=tz_iana)

        pinned = PinnedTimezone(
            tz_iana=tz_iana,
            tzinfo=tzinfo,
            source=source,
            version_label=version_label,
            tzif_sha256=tzif_sha256,
        )
        self._cache[tz_iana] = pinned
        return pinned

    def _load_tzif_bytes(self, tz_iana: str) -> tuple[bytes, str, str]:
        # 1. Walk explicit OS TZPATH directories first (matching CPython lookup order)
        for base in self._tzpath:
            base_path = Path(base)
            candidate = base_path / tz_iana
            if candidate.is_file():
                raw = candidate.read_bytes()
                version_label = self._detect_os_tzdb_version(base_path)
                return raw, f"os_tzinfo:{candidate}", version_label

        # 2. Fall back to the Python tzdata package
        parts = tz_iana.split("/")
        pkg = (
            "tzdata.zoneinfo." + ".".join(parts[:-1])
            if len(parts) > 1
            else "tzdata.zoneinfo"
        )
        try:
            resource = importlib.resources.files(pkg).joinpath(parts[-1])
            raw = resource.read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise KeyError(f"IANA timezone not found in TZPATH or tzdata: {tz_iana}") from exc

        pkg_ver = getattr(tzdata, "__version__", "unknown")
        return raw, f"python_tzdata:{tz_iana}", f"python_tzdata:{pkg_ver}"

    @staticmethod
    def _detect_os_tzdb_version(base_path: Path) -> str:
        version_file = base_path / "+VERSION"
        if version_file.is_file():
            text = version_file.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                return f"os_tzinfo:{text}"

        zi_file = base_path / "tzdata.zi"
        if zi_file.is_file():
            with zi_file.open("r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip()
            if first_line.startswith("# version "):
                return f"os_tzinfo:{first_line.removeprefix('# version ').strip()}"

        return "os_tzinfo:unlabeled"


def resolve_local_to_utc(
    local_date: date,
    local_time: time,
    pinned_tz: PinnedTimezone,
    *,
    ambiguous_policy: AmbiguousPolicy = "fold_0",
    gap_policy: GapPolicy = "shift_forward",
) -> ResolvedInstant:
    """Resolve a local wall-clock (date, time) into an exact UTC epoch second.

    - Nonexistent time (spring-forward gap):
      * gap_policy='closed'        -> returns utc_epoch=None
      * gap_policy='shift_forward' -> exact first valid post-gap UTC instant
    - Ambiguous time (fall-back overlap):
      * ambiguous_policy='fold_0'  -> earlier UTC instant (pre-transition offset)
      * ambiguous_policy='fold_1'  -> later UTC instant (post-transition offset)
    """
    tz = pinned_tz.tzinfo
    dt0 = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        local_time.hour,
        local_time.minute,
        local_time.second,
        tzinfo=tz,
        fold=0,
    )
    dt1 = dt0.replace(fold=1)

    utc0_dt = dt0.astimezone(timezone.utc)
    rt0 = utc0_dt.astimezone(tz)

    # PEP 495 round-trip check: if local wall clock changed after UTC round-trip, it's a gap.
    is_gap = rt0.replace(tzinfo=None) != dt0.replace(tzinfo=None)
    if is_gap:
        if gap_policy == "closed":
            return ResolvedInstant(
                utc_epoch=None,
                is_gap=True,
                is_ambiguous=False,
                resolution_note="NONEXISTENT_LOCAL_TIME_CLOSED",
            )

        pre_offset = dt0.utcoffset()
        post_offset = rt0.utcoffset()
        assert pre_offset is not None and post_offset is not None
        jump_sec = int((post_offset - pre_offset).total_seconds())

        # Binary search for the exact smallest UTC second with post_offset.
        hi = int(utc0_dt.timestamp())
        lo = hi - max(jump_sec, 3600) - 60
        while lo < hi:
            mid = (lo + hi) // 2
            if datetime.fromtimestamp(mid, tz).utcoffset() == post_offset:
                hi = mid
            else:
                lo = mid + 1

        return ResolvedInstant(
            utc_epoch=lo,
            is_gap=True,
            is_ambiguous=False,
            resolution_note="NONEXISTENT_LOCAL_TIME_SHIFTED_FORWARD",
        )

    u0 = int(utc0_dt.timestamp())
    u1 = int(dt1.astimezone(timezone.utc).timestamp())

    if u0 != u1:
        chosen = u0 if ambiguous_policy == "fold_0" else u1
        return ResolvedInstant(
            utc_epoch=chosen,
            is_gap=False,
            is_ambiguous=True,
            resolution_note=f"AMBIGUOUS_LOCAL_TIME_{ambiguous_policy.upper()}",
        )

    return ResolvedInstant(
        utc_epoch=u0,
        is_gap=False,
        is_ambiguous=False,
        resolution_note="EXACT",
    )


def utc_to_local(utc_epoch: int, pinned_tz: PinnedTimezone) -> datetime:
    """Convert an exact UTC epoch second to a timezone-aware local datetime."""
    return datetime.fromtimestamp(utc_epoch, tz=pinned_tz.tzinfo)
