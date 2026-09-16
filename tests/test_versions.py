import pytest

from gonogo.versions import (
    SCHEME_BASE,
    SCHEME_NONE,
    SCHEME_OFFSET,
    VersionError,
    derive_dplus,
    extract_version,
    parse_version,
)


def test_parse_version_reads_four_octets():
    version = parse_version("7.12.0.133")
    assert (version.major, version.minor, version.patch, version.build) == (7, 12, 0, 133)
    assert version.train == "7.12.0"
    assert str(version) == "7.12.0.133"


@pytest.mark.parametrize("value", ["7.12.0", "7.12.0.133.1", "v7.12.0.133", "", "latest"])
def test_parse_version_rejects_non_octet_strings(value):
    with pytest.raises(VersionError):
        parse_version(value)


def test_offset_platforms_move_to_the_21_train():
    assert derive_dplus("7.12.0.16", SCHEME_OFFSET) == "21.12.0.16"


def test_base_platforms_keep_the_max_train():
    assert derive_dplus("7.12.0.96", SCHEME_BASE) == "7.12.0.96"


def test_separate_dplus_build_number_wins():
    # Roku ships D+ from its own build count, not MAX's.
    assert derive_dplus("7.12.0.49", SCHEME_BASE, dplus_build=50) == "7.12.0.50"
    assert derive_dplus("7.12.0.73", SCHEME_OFFSET, dplus_build=16) == "21.12.0.16"


def test_clients_without_dplus_return_nothing():
    assert derive_dplus("7.12.0.73", SCHEME_NONE) is None


def test_unknown_scheme_is_rejected():
    with pytest.raises(ValueError):
        derive_dplus("7.12.0.73", "sideways")


def test_extract_version_from_artifact_name():
    assert extract_version("max-android-7.12.0.66-release.apk") == "7.12.0.66"
    assert extract_version("no-version-here.apk") is None
