"""Version parsing and MAX -> D+ derivation for Fuse multi-brand builds.

MAX tracks the base unified Fuse version (``7.x.y.build``). Apple, Android
mobile and Android TV ship D+ from the store-offset scheme (``21.x.y.build``);
Web, CDEV, Chromecast and Roku ship D+ on the same base scheme as MAX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 7.x -> 21.x for platforms that carry a store version offset.
DPLUS_MAJOR_OFFSET = 14

#: How a client row derives its D+ version from the MAX version.
SCHEME_BASE = "base"      # same major as MAX (7.x.y.build)
SCHEME_OFFSET = "offset"  # major + 14 (21.x.y.build)
SCHEME_NONE = "none"      # brand not shipped on this client; cell left alone

SCHEMES = (SCHEME_BASE, SCHEME_OFFSET, SCHEME_NONE)

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\.(\d+)\s*$")


class VersionError(ValueError):
    """Raised when a build string is not a 4-segment Fuse version."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    build: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}.{self.build}"

    @property
    def train(self) -> str:
        """The 3-segment release train, e.g. ``7.12.0``."""
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(value: str) -> Version:
    """Parse a 4-segment octet version string such as ``7.12.0.133``."""
    match = _VERSION_RE.match(value or "")
    if not match:
        raise VersionError(f"not a 4-segment version: {value!r}")
    return Version(*(int(part) for part in match.groups()))


def is_version(value: str) -> bool:
    return bool(_VERSION_RE.match(value or ""))


def expected_dplus_major(max_version: str, scheme: str) -> int | None:
    """The major a D+ version should carry for this scheme, or None if unshipped."""
    if scheme not in SCHEMES:
        raise ValueError(f"unknown dplus scheme {scheme!r}, expected one of {SCHEMES}")
    if scheme == SCHEME_NONE:
        return None
    major = parse_version(max_version).major
    return major + DPLUS_MAJOR_OFFSET if scheme == SCHEME_OFFSET else major


def derive_dplus(
    max_version: str,
    scheme: str,
    dplus_build: int | None = None,
    assume_max_build: bool = False,
) -> str | None:
    """Return the D+ version string for a client row.

    Without a reported ``dplus_build`` this returns ``None`` rather than borrow
    MAX's octet, which would name a build that was never produced: iOS on 7.12.0
    is MAX ``7.12.0.73`` against a real D+ of ``21.12.0.16``. ``assume_max_build``
    opts into that guess for a feed carrying only MAX.
    """
    if scheme not in SCHEMES:
        raise ValueError(f"unknown dplus scheme {scheme!r}, expected one of {SCHEMES}")
    if scheme == SCHEME_NONE:
        return None

    version = parse_version(max_version)
    if dplus_build is None:
        if not assume_max_build:
            return None
        dplus_build = version.build
    major = version.major + DPLUS_MAJOR_OFFSET if scheme == SCHEME_OFFSET else version.major
    return str(Version(major, version.minor, version.patch, dplus_build))


def extract_version(text: str) -> str | None:
    """Pull the first 4-segment version out of free text such as a filename."""
    match = re.search(r"\d+\.\d+\.\d+\.\d+", text or "")
    return match.group(0) if match else None


def extract_train(text: str) -> str | None:
    """Pull a 3-segment train out of free text such as a page title.

    ``Copy of 7.12.0 Build GQA App Sign off`` yields ``7.12.0``. A 4-segment
    build number is not a train and is ignored.
    """
    match = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", text or "")
    return match.group(1) if match else None
