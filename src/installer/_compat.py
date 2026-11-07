"""Cross-target compatibility checks for wheel installation.

Build tools installing wheels into an environment other than the one running
``installer`` describe the *target* environment explicitly (ordered wheel tag
list, exact Python version and launcher kind). Nothing in this module inspects
the host platform, spawns a target interpreter or resolves/installs target
dependencies.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from installer._core import _install, _process_WHEEL_file
from installer.exceptions import (
    InvalidRequiresPython,
    InvalidTarget,
    InvalidWheelSource,
    InvalidWheelTags,
    NoCompatibleTags,
    UnsupportedPythonVersion,
    WheelTagMismatch,
)
from installer.scripts import LAUNCHER_KINDS
from installer.utils import parse_metadata_file

if TYPE_CHECKING:
    from collections.abc import Sequence

    from installer.destinations import WheelDestination
    from installer.scripts import LauncherKind
    from installer.sources import WheelSource

__all__ = ["Target", "check_compatibility", "install_for_target"]


# ---------------------------------------------------------------------------
# Target description
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    """Description of the Python environment a wheel is being installed for.

    :param tags: wheel tags supported by the target, ordered by decreasing
        preference (equivalent to the output of pip's supported-tag list).
        Compressed tag sets, using ``.`` within the interpreter/ABI/platform
        components, are accepted.
    :param python_version: exact target Python version, as a PEP 440 version
        string that includes the patch component (e.g. ``"3.11.4"`` or
        ``"3.12.0rc1"``). The host interpreter's version is never used.
    :param launcher_kind: kind of launcher to generate for the target's
        scripts, which must be compatible with the target's platform tags.
    """

    tags: Sequence[str]
    python_version: str
    launcher_kind: LauncherKind


# ---------------------------------------------------------------------------
# Wheel tag handling (PEP 425)
# ---------------------------------------------------------------------------
_TAG_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Platform tags corresponding to each Windows launcher kind.
_LAUNCHER_KIND_PLATFORM: dict[str, str] = {
    "win-ia32": "win32",
    "win-amd64": "win_amd64",
    "win-arm": "win_arm",
    "win-arm64": "win_arm64",
}


def _tag_parts(
    tag: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split a (possibly compressed) tag into its dot-expanded components."""
    parts = tag.split("-")
    if len(parts) != 3:
        message = "tag must have the form 'interpreter-abi-platform': {!r}"
        raise ValueError(message.format(tag))

    expanded_parts: list[tuple[str, ...]] = []
    for part in parts:
        if not part:
            raise ValueError(f"empty component in tag {tag!r}")
        segments = tuple(part.split("."))
        if any(
            not segment or _TAG_SEGMENT_RE.fullmatch(segment) is None
            for segment in segments
        ):
            raise ValueError(f"invalid compressed tag component {part!r} in {tag!r}")
        expanded_parts.append(segments)

    return expanded_parts[0], expanded_parts[1], expanded_parts[2]


def _expand_compressed_tag(tag: str) -> frozenset[str]:
    """Cartesian-expand a compressed interpreter/ABI/platform tag."""
    interpreters, abis, platforms = _tag_parts(tag)
    return frozenset(
        f"{interpreter}-{abi}-{platform}"
        for interpreter in interpreters
        for abi in abis
        for platform in platforms
    )


def _wheel_tag_set(source: WheelSource) -> frozenset[str]:
    """Get the wheel's tag set, cross-checking filename against WHEEL metadata."""
    wheel_metadata = parse_metadata_file(source.read_dist_info("WHEEL"))

    wheel_version = wheel_metadata["Wheel-Version"]
    if not (wheel_version and wheel_version.startswith("1.")):
        message = "Incompatible Wheel-Version {}, only support version 1.x wheels."
        raise InvalidWheelSource(source, message.format(wheel_version))

    try:
        filename_tags = _expand_compressed_tag(source.wheel_tag)
    except ValueError as exc:
        raise InvalidWheelTags(
            f"Invalid tag in wheel filename {source.wheel_tag!r}: {exc}"
        ) from exc

    tag_fields = wheel_metadata.get_all("Tag")
    if not tag_fields:
        message = f"WHEEL metadata of {source.distribution!r} does not declare any Tag"
        raise InvalidWheelTags(message)

    wheel_tags: set[str] = set()
    for tag_field in tag_fields:
        compressed_tag = tag_field.strip()
        try:
            wheel_tags.update(_expand_compressed_tag(compressed_tag))
        except ValueError as exc:
            raise InvalidWheelTags(
                f"Invalid Tag {compressed_tag!r} in WHEEL metadata: {exc}"
            ) from exc

    wheel_tag_set = frozenset(wheel_tags)
    if wheel_tag_set != filename_tags:
        message = (
            f"Tags in wheel filename ({sorted(filename_tags)}) do not match "
            f"the Tag fields in WHEEL ({sorted(wheel_tag_set)})"
        )
        raise WheelTagMismatch(message)
    return wheel_tag_set


# ---------------------------------------------------------------------------
# Minimal PEP 440 version + specifier handling, for Requires-Python.
# ---------------------------------------------------------------------------
_VERSION_RE = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [-_.]?
        (?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)
        [-_.]?
        (?P<pre_n>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:
            [-_.]?
            (?P<post_l>post|rev|r)
            [-_.]?
            (?P<post_n2>[0-9]+)?
        )
    )?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PRE_LETTERS = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "c": "rc",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
}
_PRE_PHASE = {"a": 0, "b": 1, "rc": 2}

_VersionKey = tuple[
    int,
    tuple[int, ...],
    tuple[int, int],
    tuple[int, int],
    tuple[int, int],
    tuple[int, tuple[tuple[int, tuple[int, ...]], ...]],
]


@dataclass(frozen=True, eq=False)
class _Version:
    """Parsed PEP 440 version, ordered using the PEP 440 comparison rules."""

    epoch: int
    raw_release: tuple[int, ...]
    pre: tuple[int, int]
    post: tuple[int, int]
    dev: tuple[int, int]
    local: tuple[int, tuple[tuple[int, tuple[int, ...]], ...]]

    @staticmethod
    def _local_segment(text: str) -> tuple[int, tuple[int, ...]]:
        if text.isdigit():
            return (1, (int(text),))
        return (0, tuple(ord(char) for char in text.lower()))

    @property
    def release(self) -> tuple[int, ...]:
        return tuple(
            reversed(
                list(itertools.dropwhile(lambda x: x == 0, reversed(self.raw_release)))
            )
        )

    @property
    def _key(self) -> _VersionKey:
        return self.epoch, self.release, self.pre, self.post, self.dev, self.local

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Version):
            return NotImplemented
        return self._key == other._key

    def __lt__(self, other: _Version) -> bool:
        return self._key < other._key

    def __le__(self, other: _Version) -> bool:
        return self._key <= other._key

    def __gt__(self, other: _Version) -> bool:
        return self._key > other._key

    def __ge__(self, other: _Version) -> bool:
        return self._key >= other._key

    def __hash__(self) -> int:
        return hash(self._key)


def _required_str(value: str | None) -> str:
    """Narrow a regex group guaranteed to participate in an overall match."""
    assert value is not None
    return value


def _parse_version(text: str) -> _Version:
    """Parse a PEP 440 version, raising ``ValueError`` if it isn't one."""
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a valid PEP 440 version: {text!r}")

    epoch = int(match.group("epoch") or 0)
    raw_release = tuple(
        int(part) for part in _required_str(match.group("release")).split(".")
    )

    pre_letter = match.group("pre_l")
    if pre_letter is None:
        pre: tuple[int, int] | None = None
    else:
        phase = _PRE_PHASE[_PRE_LETTERS[pre_letter.lower()]]
        pre = (phase, int(match.group("pre_n") or 0))

    post_number = match.group("post_n1") or match.group("post_n2")
    post = (1, int(post_number)) if post_number is not None else (0, 0)

    has_dev = match.group("dev") is not None
    dev = (0, int(match.group("dev_n") or 0)) if has_dev else (1, 0)

    if pre is None and post == (0, 0) and has_dev:
        pre = (-1, 0)
    elif pre is None:
        pre = (3, 0)

    local_text = match.group("local")
    if local_text is None:
        local: tuple[int, tuple[tuple[int, tuple[int, ...]], ...]] = (0, ())
    else:
        segments = tuple(
            _Version._local_segment(part) for part in re.split(r"[-_.]", local_text)
        )
        local = (1, segments)

    return _Version(epoch, raw_release, pre, post, dev, local)


_SPECIFIER_RE = re.compile(r"^(===|~=|==|!=|<=|>=|<|>)\s*(\S.*?)\s*$")


def _specifier_satisfied(version: _Version, clause: str) -> bool:
    """Check one PEP 440 version specifier clause, raising if malformed."""
    match = _SPECIFIER_RE.fullmatch(clause)
    if match is None:
        raise InvalidRequiresPython(f"invalid version specifier clause: {clause!r}")

    operator = _required_str(match.group(1))
    if operator == "===":
        message = f"arbitrary equality ('===') is not supported in {clause!r}"
        raise InvalidRequiresPython(message)

    spec_text = _required_str(match.group(2))
    is_prefix = spec_text.endswith(".*")
    if is_prefix:
        if operator not in ("==", "!="):
            message = f"version prefix matching ('*') requires '==' or '!=': {clause!r}"
            raise InvalidRequiresPython(message)
        spec_text = spec_text[:-2]

    try:
        spec = _parse_version(spec_text)
    except ValueError as exc:
        raise InvalidRequiresPython(f"invalid version in specifier {clause!r}") from exc

    if is_prefix:
        if (
            spec.pre != (3, 0)
            or spec.post != (0, 0)
            or spec.dev != (1, 0)
            or spec.local[0] != 0
        ):
            message = f"prefix specifier may only use release segments: {clause!r}"
            raise InvalidRequiresPython(message)
        matches_prefix = (
            version.epoch == spec.epoch
            and version.raw_release[: len(spec.raw_release)] == spec.raw_release
        )
        return matches_prefix if operator == "==" else not matches_prefix

    if operator == "~=":
        if len(spec.raw_release) < 2:
            message = f"'~=' requires a version with at least two segments: {clause!r}"
            raise InvalidRequiresPython(message)
        prefix = spec.raw_release[:-1]
        return (
            version >= spec
            and version.epoch == spec.epoch
            and version.raw_release[: len(prefix)] == prefix
        )

    if operator == "==":
        return version == spec
    if operator == "!=":
        return version != spec
    if operator == "<":
        return version < spec
    if operator == "<=":
        return version <= spec
    if operator == ">":
        return version > spec
    return version >= spec


def _check_requires_python(
    specifier_set: str, target_version_text: str, target_version: _Version
) -> None:
    """Validate the exact target version against a Requires-Python constraint."""
    text = specifier_set.strip()
    if not text:
        raise InvalidRequiresPython("Requires-Python metadata is empty")

    clauses = [clause.strip() for clause in text.split(",")]
    for clause in clauses:
        if not clause:
            raise InvalidRequiresPython(
                f"empty specifier clause in Requires-Python {specifier_set!r}"
            )
        if not _specifier_satisfied(target_version, clause):
            message = (
                f"target Python {target_version_text!r} does not satisfy "
                f"Requires-Python {specifier_set!r}"
            )
            raise UnsupportedPythonVersion(message)


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------
def _parse_target_python(python_version: str) -> _Version:
    """Parse the exact target Python version, requiring a full release."""
    try:
        version = _parse_version(python_version)
    except ValueError as exc:
        raise InvalidTarget(
            f"invalid target Python version {python_version!r}: {exc}"
        ) from exc
    if len(version.raw_release) < 3:
        message = (
            f"target Python version {python_version!r} must specify the exact "
            "version, including its patch level (e.g. '3.11.4')"
        )
        raise InvalidTarget(message)
    return version


def _validate_target(
    target: Target,
) -> tuple[list[str], frozenset[str], _Version]:
    """Validate the target description itself.

    Returns the ordered, de-duplicated and expanded target tags, the set of
    platforms covered by them, and the parsed target Python version.
    """
    if isinstance(target.tags, str) or not target.tags:
        message = "target tags must be a non-empty ordered sequence of wheel tags"
        raise InvalidTarget(message)

    ordered_tags: list[str] = []
    seen_tags: set[str] = set()
    platforms: set[str] = set()
    for compressed_tag in target.tags:
        if not isinstance(compressed_tag, str):
            raise InvalidTarget(
                f"target wheel tags must be strings, got {compressed_tag!r}"
            )
        try:
            interpreters, abis, tag_platforms = _tag_parts(compressed_tag)
        except ValueError as exc:
            raise InvalidTarget(
                f"invalid target wheel tag {compressed_tag!r}: {exc}"
            ) from exc
        platforms.update(tag_platforms)
        for interpreter in interpreters:
            for abi in abis:
                for platform in tag_platforms:
                    expanded = f"{interpreter}-{abi}-{platform}"
                    if expanded not in seen_tags:
                        seen_tags.add(expanded)
                        ordered_tags.append(expanded)

    version = _parse_target_python(target.python_version)

    kind = target.launcher_kind
    windows_platforms = {
        platform for platform in platforms if platform.startswith("win")
    }
    if kind == "posix":
        if windows_platforms:
            message = (
                "launcher kind 'posix' is incompatible with Windows target "
                f"platform tags {sorted(windows_platforms)}"
            )
            raise InvalidTarget(message)
    else:
        required_platform = _LAUNCHER_KIND_PLATFORM.get(kind)
        if required_platform is None:
            message = (
                f"unknown launcher kind {kind!r}; expected one of {LAUNCHER_KINDS}"
            )
            raise InvalidTarget(message)
        if required_platform not in platforms:
            message = (
                f"launcher kind {kind!r} requires target platform tag "
                f"{required_platform!r}, which is absent from the target tags"
            )
            raise InvalidTarget(message)
        non_windows_platforms = platforms - windows_platforms - {"any"}
        if non_windows_platforms:
            message = (
                "target platform tags mix Windows with non-Windows platforms: "
                f"{sorted(windows_platforms)} and {sorted(non_windows_platforms)}"
            )
            raise InvalidTarget(message)

    return ordered_tags, frozenset(platforms), version


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def check_compatibility(source: WheelSource, target: Target) -> str:
    """Check that ``source`` is compatible with the described ``target``.

    Only reads wheel metadata; it never writes to a destination, inspects the
    host platform or runs the target interpreter.

    :raises InvalidWheelSource: if the wheel's ``Wheel-Version`` is unsupported.
    :raises InvalidWheelTags: if a tag is missing or syntactically invalid.
    :raises WheelTagMismatch: if the expanded filename tags differ from the
        ``Tag`` fields in the ``WHEEL`` file.
    :raises NoCompatibleTags: if no target tag exactly matches a wheel tag.
    :raises UnsupportedPythonVersion: if the exact target version does not
        satisfy the wheel's ``Requires-Python`` constraint.
    :raises InvalidRequiresPython: if ``Requires-Python`` is malformed.
    :raises InvalidTarget: if the target description is invalid.

    :return: the first (highest-priority) target tag that matches a wheel tag.
    """
    ordered_target_tags, _target_platforms, target_version = _validate_target(target)
    wheel_tags = _wheel_tag_set(source)

    matched_tag: str | None = None
    for target_tag in ordered_target_tags:
        if target_tag in wheel_tags:
            matched_tag = target_tag
            break
    if matched_tag is None:
        message = (
            f"none of the target tags {list(target.tags)} match the wheel tags "
            f"{sorted(wheel_tags)}"
        )
        raise NoCompatibleTags(message)

    package_metadata = parse_metadata_file(source.read_dist_info("METADATA"))
    requires_python = package_metadata.get("Requires-Python")
    if requires_python is not None:
        _check_requires_python(requires_python, target.python_version, target_version)

    return matched_tag


def install_for_target(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
    target: Target,
) -> str:
    """Install ``source`` only if it is compatible with the described ``target``.

    All compatibility checks run before any method on ``destination`` is
    invoked. On success, this performs the same file writes as
    :any:`installer.install` and returns the highest-priority matching tag.

    :param source: wheel to install.
    :param destination: where to write the wheel. If it exposes a
        ``script_kind`` attribute, it must equal ``target.launcher_kind``.
    :param additional_metadata: additional metadata files to generate.
    :param target: explicit description of the target environment.

    :raises installer.exceptions.InstallerError: if a compatibility check fails.
    :return: the first (highest-priority) target tag that matches a wheel tag.
    """
    matched_tag = check_compatibility(source, target)

    # Destinations such as SchemeDictionaryDestination decide the launcher kind
    # themselves; make sure they cannot generate launchers for another platform.
    destination_type = type(destination)
    declares_kind = "script_kind" in getattr(
        destination_type, "__annotations__", {}
    ) or hasattr(destination_type, "script_kind")
    if declares_kind:
        destination_kind: object = getattr(destination, "script_kind", None)
        if destination_kind != target.launcher_kind:
            message = (
                f"destination launcher kind {destination_kind!r} does not match "
                f"target launcher kind {target.launcher_kind!r}"
            )
            raise InvalidTarget(message)

    root_scheme = _process_WHEEL_file(source)
    _install(source, destination, additional_metadata, root_scheme)
    return matched_tag
