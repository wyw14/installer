"""Selective installation of wheel contents, via named components.

Callers split the regular files of a wheel into named *components*, using
explicit POSIX-relative path patterns, and choose which components to
install. Files that no declared rule matches are either kept or excluded,
according to a caller-chosen default policy.

Patterns support:

- ``*`` and ``?`` within a single path segment (they never match ``/``), and
- a standalone ``**`` path segment, matching zero or more path segments.

Patterns never touch the host file system, do not follow links, and may only
describe POSIX-relative paths: absolute paths, ``..`` (and ``.``) segments,
backslashes and ``**`` embedded inside a segment are all rejected.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from configparser import ConfigParser
from dataclasses import dataclass, field
from re import Pattern
from typing import Literal

from installer.exceptions import InvalidComponents

__all__ = ["Components"]

_DEFAULT_POLICIES = ("keep", "exclude")
_SCRIPT_SECTIONS = frozenset({"console_scripts", "gui_scripts"})
# Entry-point sections, keyed by the short names parse_entrypoints() yields.
_ENTRY_POINT_SECTION_NAMES = {
    "console": "console_scripts",
    "gui": "gui_scripts",
}
_GLOBSTAR = "**"

# A syntactically valid dotted module path (letter/underscore led, dotted).
_MODULE_NAME_REGEX = re.compile(r"^[^\W\d]\w*(?:\.[^\W\d]\w*)*$", re.UNICODE)

# A ``[section]`` header, as understood by configparser.
_SECTION_HEADER_REGEX = re.compile(r"^\s*\[(?P<name>.+)\]\s*$")

# An ``option = value`` line, using ``=`` as the only delimiter.
_OPTION_LINE_REGEX = re.compile(r"^\s*(?P<name>[^=:;#\s][^=:]*?)\s*=")

# A compiled pattern is a sequence of per-segment matchers, where ``None``
# stands for a standalone ``**`` segment (zero or more path segments).
_CompiledPattern = tuple[Pattern[str] | None, ...]


def validate_pattern(pattern: str) -> None:
    """Validate a component path pattern.

    :param pattern: POSIX-relative wheel path pattern to validate
    :raises InvalidComponents: if the pattern is not a valid explicit
        POSIX-relative path or uses glob constructs outside the supported
        ``*``, ``?`` and standalone ``**`` syntax.
    """
    if not pattern:
        raise InvalidComponents("Component patterns cannot be empty.")

    if "\\" in pattern:
        message = (
            f"Component pattern {pattern!r} must use POSIX '/' separators; "
            "backslashes are not allowed."
        )
        raise InvalidComponents(message)

    if pattern.startswith("/"):
        message = (
            f"Component pattern {pattern!r} must be relative; absolute paths "
            "are not allowed."
        )
        raise InvalidComponents(message)

    for segment in pattern.split("/"):
        if not segment:
            message = f"Component pattern {pattern!r} contains an empty path segment."
            raise InvalidComponents(message)
        if segment in (".", ".."):
            message = (
                f"Component pattern {pattern!r} must be an explicit relative "
                "path; '.' and '..' segments are not allowed."
            )
            raise InvalidComponents(message)
        if _GLOBSTAR in segment and segment != _GLOBSTAR:
            message = (
                f"Component pattern {pattern!r} is invalid: '**' is only "
                "supported as a standalone path segment."
            )
            raise InvalidComponents(message)


def _translate_segment(segment: str) -> Pattern[str]:
    parts = []
    for char in segment:
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(out for out in parts))


def _compile_pattern(pattern: str) -> _CompiledPattern:
    validate_pattern(pattern)
    return tuple(
        None if segment == _GLOBSTAR else _translate_segment(segment)
        for segment in pattern.split("/")
    )


def _pattern_matches(compiled: _CompiledPattern, segments: Sequence[str]) -> bool:
    def match_from(pattern_index: int, path_index: int) -> bool:
        while pattern_index < len(compiled):
            segment_matcher = compiled[pattern_index]
            if segment_matcher is None:
                # A standalone '**' may consume zero or more path segments.
                return any(
                    match_from(pattern_index + 1, consumed)
                    for consumed in range(path_index, len(segments) + 1)
                )
            if path_index >= len(segments):
                return False
            if segment_matcher.fullmatch(segments[path_index]) is None:
                return False
            pattern_index += 1
            path_index += 1
        return path_index == len(segments)

    return match_from(0, 0)


def module_file_candidates(module: str) -> tuple[str, str]:
    """Return scheme-relative source paths backing a Python module.

    :param module: dotted module path (e.g. ``"fancy.cli"``)
    :return: the module's ``.py`` file and its package ``__init__.py`` path
    """
    base = module.replace(".", "/")
    return f"{base}.py", f"{base}/__init__.py"


def rewrite_entry_points(
    text: str,
    kept_entry_points: Collection[tuple[str, str]],
) -> str:
    """Rewrite ``entry_points.txt`` contents, dropping unselected script entries.

    Only ``console_scripts`` and ``gui_scripts`` entries may be removed; an
    entry in those sections is retained when its ``(section, name)`` pair is
    in ``kept_entry_points``. Every other section is preserved byte-for-byte.

    :param text: the original ``entry_points.txt`` contents
    :param kept_entry_points: ``(section, name)`` pairs of script entries to
        keep, where section is the short name ``"console"`` or ``"gui"`` as
        yielded by :func:`installer.utils.parse_entrypoints`
    :return: the rewritten contents, which may be empty
    """
    kept = {
        (_ENTRY_POINT_SECTION_NAMES[section], name)
        for section, name in kept_entry_points
    }
    config = ConfigParser(delimiters="=")
    config.optionxform = str  # type: ignore[assignment, method-assign]
    config.read_string(text)
    options_by_section = {
        section: {name for name, _value in config.items(section)}
        for section in config.sections()
        if section in _SCRIPT_SECTIONS
    }

    output: list[str] = []
    current_section: str | None = None
    buffered_section: list[str] = []
    kept_in_current = 0

    def flush_script_section() -> None:
        nonlocal buffered_section, kept_in_current
        if current_section in _SCRIPT_SECTIONS and kept_in_current:
            output.extend(buffered_section)
        buffered_section = []
        kept_in_current = 0

    for line in text.splitlines(keepends=True):
        header_match = _SECTION_HEADER_REGEX.match(line)
        if header_match is not None:
            flush_script_section()
            current_section = None
            section_name = header_match.group("name").strip()
            if section_name in _SCRIPT_SECTIONS:
                current_section = section_name
                buffered_section = [line]
            else:
                # Unknown section: preserve untouched.
                output.append(line)
        elif current_section is None:
            # Preamble content or the body of an unknown section.
            output.append(line)
        else:
            option_match = _OPTION_LINE_REGEX.match(line)
            option_name = (
                option_match.group("name").strip() if option_match is not None else None
            )
            known_option = (
                option_name is not None
                and option_name in options_by_section.get(current_section, set())
            )
            if not known_option or (current_section, option_name) in kept:
                buffered_section.append(line)
                if known_option:
                    kept_in_current += 1

    flush_script_section()
    return "".join(output)


@dataclass(frozen=True)
class Components:
    """Describe named installation components and which ones to install.

    Every regular file in the wheel is matched against the wheel-relative
    POSIX path patterns in :attr:`rules`; matching happens before ``.data``
    relocation. A file matched by a component in :attr:`selected` is
    installed; components may overlap, and each source file is installed at
    most once. Files that no rule matches follow :attr:`default`.

    The ``METADATA`` and ``WHEEL`` files of the wheel's ``.dist-info``
    directory, as well as the generated ``RECORD`` file, are always
    installed and cannot be excluded.
    """

    rules: Mapping[str, Collection[str]] = field(default_factory=dict)
    """Mapping of component names to wheel-relative POSIX path patterns."""

    selected: Collection[str] | None = None
    """Components to install. ``None`` selects every component in :attr:`rules`."""

    default: Literal["keep", "exclude"] = "keep"
    """Whether files matched by no rule are kept (``"keep"``) or excluded."""

    entry_points: Mapping[str, str] = field(default_factory=dict)
    """Mapping of console/gui entry point names to the component they belong to.

    Entry points absent from this mapping follow :attr:`default`.
    """

    exempt_modules: Collection[str] = frozenset()
    """Dotted module paths of kept entry points to skip static checks for.

    Use this for entry points backed by extension modules whose presence
    cannot be determined from the wheel's ``.py`` files. Modules are never
    imported to perform the check.
    """

    def __post_init__(self) -> None:
        """Validate the whole configuration, so errors surface before writes."""
        self._validate()

    def _validate(self) -> None:
        rules = _validate_rules(self.rules)

        if self.default not in _DEFAULT_POLICIES:
            allowed = ", ".join(repr(policy) for policy in _DEFAULT_POLICIES)
            raise InvalidComponents(
                f"Components.default must be one of {allowed}, got {self.default!r}."
            )

        _validate_selected(self.selected, rules)
        _validate_entry_point_mapping(self.entry_points, rules)
        _validate_exempt_modules(self.exempt_modules)


def _validate_rules(value: object) -> set[str]:
    """Validate ``Components.rules`` and return the declared component names."""
    if not isinstance(value, Mapping):
        raise InvalidComponents(
            "Components.rules must be a mapping of component names to "
            "path pattern collections."
        )

    component_names: set[str] = set()
    for component, patterns in value.items():
        if not isinstance(component, str) or not component:
            raise InvalidComponents(
                f"Component names must be non-empty strings, got {component!r}."
            )
        if isinstance(patterns, str) or not isinstance(patterns, Collection):
            raise InvalidComponents(
                f"Rules for component {component!r} must be a collection of "
                "path pattern strings."
            )
        for pattern in patterns:
            validate_pattern(pattern)
        component_names.add(component)
    return component_names


def _validate_selected(value: object, component_names: Collection[str]) -> None:
    """Validate ``Components.selected`` against the declared names."""
    if value is None:
        return
    if isinstance(value, str) or not isinstance(value, Collection):
        raise InvalidComponents(
            "Components.selected must be a collection of component names, or None."
        )
    for component in value:
        if not isinstance(component, str) or not component:
            raise InvalidComponents(
                "Selected component names must be non-empty strings, "
                f"got {component!r}."
            )
        if component not in component_names:
            raise InvalidComponents(
                f"Selected component {component!r} does not match any "
                "declared component in Components.rules."
            )


def _validate_entry_point_mapping(
    value: object, component_names: Collection[str]
) -> None:
    """Validate ``Components.entry_points`` against the declared names."""
    if not isinstance(value, Mapping):
        raise InvalidComponents(
            "Components.entry_points must be a mapping of entry point names "
            "to component names."
        )
    for entry_point, component in value.items():
        if not isinstance(entry_point, str) or not entry_point:
            raise InvalidComponents(
                f"Entry point names must be non-empty strings, got {entry_point!r}."
            )
        if not isinstance(component, str) or component not in component_names:
            raise InvalidComponents(
                f"Entry point {entry_point!r} is assigned to component "
                f"{component!r}, which is not declared in Components.rules."
            )


def _validate_exempt_modules(value: object) -> None:
    """Validate ``Components.exempt_modules``."""
    if isinstance(value, str) or not isinstance(value, Collection):
        raise InvalidComponents(
            "Components.exempt_modules must be a collection of dotted "
            "module path strings."
        )
    for module in value:
        if not isinstance(module, str) or not _MODULE_NAME_REGEX.fullmatch(module):
            raise InvalidComponents(
                f"{module!r} is not a valid dotted module path for "
                "Components.exempt_modules."
            )


class _Selector:
    """Apply a validated :class:`Components` configuration to wheel paths."""

    def __init__(self, config: Components) -> None:
        self._config = config
        self._compiled_rules: Mapping[str, tuple[_CompiledPattern, ...]] = {
            component: tuple(_compile_pattern(pattern) for pattern in patterns)
            for component, patterns in config.rules.items()
        }
        if config.selected is None:
            self.selected: frozenset[str] = frozenset(config.rules)
        else:
            self.selected = frozenset(config.selected)

    def matched_components(self, path: str) -> frozenset[str]:
        """Return the names of all components whose rules match ``path``."""
        segments = path.split("/")
        matched = {
            component
            for component, patterns in self._compiled_rules.items()
            if any(_pattern_matches(pattern, segments) for pattern in patterns)
        }
        return frozenset(matched)

    def keep_path(self, path: str) -> bool:
        """Whether a wheel-relative regular file should be installed."""
        matched = self.matched_components(path)
        if matched:
            return bool(matched & self.selected)
        return self._config.default == "keep"

    def keep_entry_point(self, name: str) -> bool:
        """Whether a console/gui entry point should be installed."""
        component = self._config.entry_points.get(name)
        if component is None:
            return self._config.default == "keep"
        return component in self.selected
