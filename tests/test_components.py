"""Tests for installer.components"""

import pytest

from installer.components import (
    Components,
    _compile_pattern,
    _pattern_matches,
    _Selector,
    module_file_candidates,
    rewrite_entry_points,
    validate_pattern,
)
from installer.exceptions import InvalidComponents


class TestValidatePattern:
    @pytest.mark.parametrize(
        "pattern",
        [
            "fancy/__init__.py",
            "*.py",
            "fancy/?est.py",
            "fancy/**",
            "**/test.py",
            "fancy/**/test.py",
            "**",
            "fancy/a?b/c*.py",
            "fancy/数据.txt",
            "ünïcode/__init__.py",
        ],
    )
    def test_valid_patterns(self, pattern):
        validate_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            "",  # empty
            "/fancy/__init__.py",  # absolute
            "fancy/../evil.py",  # parent traversal
            "../evil.py",
            "fancy/./__init__.py",  # explicit current-directory segment
            "fancy\\__init__.py",  # backslash separator
            "fancy/a**b.py",  # embedded globstar
            "fancy/**.py",
            "**b",
            "fancy//test.py",  # empty segment
            "fancy/test.py/",  # trailing separator / empty segment
        ],
    )
    def test_invalid_patterns(self, pattern):
        with pytest.raises(InvalidComponents):
            validate_pattern(pattern)


class TestPatternMatching:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            # Literal paths, including Unicode.
            ("fancy/__init__.py", "fancy/__init__.py", True),
            ("fancy/数据.txt", "fancy/数据.txt", True),
            ("fancy/数据.txt", "fancy/其他.txt", False),
            # * only matches within a single path segment.
            ("*.py", "test.py", True),
            ("*.py", "fancy/test.py", False),
            ("fancy/*.py", "fancy/test.py", True),
            ("fancy/*.py", "fancy/sub/test.py", False),
            ("fancy*", "fancy-1.0.0.data/data/x", False),
            # ? matches exactly one character within a segment.
            ("fancy/a?.py", "fancy/a1.py", True),
            ("fancy/a?.py", "fancy/a.py", False),
            ("fancy/a?.py", "fancy/a12.py", False),
            ("fancy/?", "fancy/a", True),
            ("fancy/?", "fancy/ab", False),
            # Standalone ** matches zero or more path segments.
            ("**/test.py", "test.py", True),
            ("**/test.py", "fancy/test.py", True),
            ("**/test.py", "fancy/sub/test.py", True),
            ("fancy/**", "fancy/test.py", True),
            ("fancy/**", "fancy/sub/test.py", True),
            ("fancy/**", "fancy", True),
            ("fancy/**", "other/test.py", False),
            ("a/**/z", "a/z", True),
            ("a/**/z", "a/b/z", True),
            ("a/**/z", "a/b/c/z", True),
            ("a/**/z", "a/b/c", False),
            ("**", "anything/at/all.py", True),
            # Pre-.data-relocation paths are matched verbatim.
            (
                "fancy-1.0.0.data/data/**",
                "fancy-1.0.0.data/data/fancy/data.py",
                True,
            ),
            (
                "fancy-1.0.0.data/data/**",
                "fancy-1.0.0.data/scripts/fancy/x.py",
                False,
            ),
        ],
    )
    def test_matching(self, pattern, path, expected):
        compiled = _compile_pattern(pattern)
        assert _pattern_matches(compiled, path.split("/")) is expected

    def test_matching_is_order_independent(self):
        paths = [
            "pkg/core/a.py",
            "pkg/core/sub/b.py",
            "pkg/other/c.txt",
            "other/d.py",
        ]
        rules_a = {
            "core": ["pkg/core/**"],
            "allpkg": ["pkg/**"],
            "top": ["*.py"],
        }
        rules_b = {
            "top": ["*.py"],
            "allpkg": ["pkg/**"],
            "core": ["pkg/core/**"],
        }
        selector_a = _Selector(Components(rules=rules_a))
        selector_b = _Selector(Components(rules=rules_b))
        for path in paths:
            assert selector_a.matched_components(path) == selector_b.matched_components(
                path
            )


class TestComponentsValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"rules": {"a": {"/abs/path"}}}, id="absolute-pattern"),
            pytest.param({"rules": {"a": {"../x"}}}, id="parent-segment"),
            pytest.param({"rules": {"a": {"a\\b.py"}}}, id="backslash"),
            pytest.param({"rules": {"a": {"a/**b"}}}, id="embedded-globstar"),
            pytest.param({"rules": {"a": {""}}}, id="empty-pattern"),
            pytest.param({"rules": {"": {"x"}}}, id="empty-component-name"),
            pytest.param(
                {"rules": {"a": ["x"]}, "selected": ["b"]},
                id="unknown-selected",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "entry_points": {"ep": "b"}},
                id="unknown-entry-component",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "default": "maybe"},
                id="bad-default-policy",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "exempt_modules": ["not a module!"]},
                id="bad-exempt-module",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "exempt_modules": ["0bad"]},
                id="digit-led-module",
            ),
        ],
    )
    def test_invalid_configuration(self, kwargs):
        with pytest.raises(InvalidComponents):
            Components(**kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"rules": [("a", ["x"])]}, id="rules-not-mapping"),
            pytest.param({"rules": {"a": "x"}}, id="rules-as-string"),
            pytest.param(
                {"rules": {"a": ["x"]}, "selected": "a"},
                id="selected-as-string",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "selected": [b"a"]},
                id="selected-non-string-entry",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "entry_points": [("ep", "a")]},
                id="entry-points-not-mapping",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "entry_points": {"": "a"}},
                id="empty-entry-point-name",
            ),
            pytest.param(
                {"rules": {"a": ["x"]}, "exempt_modules": "fancy.cli"},
                id="exempt-modules-as-string",
            ),
        ],
    )
    def test_rejects_wrong_runtime_types(self, kwargs):
        with pytest.raises(InvalidComponents):
            Components(**kwargs)

    def test_valid_empty_configuration(self):
        Components()

    def test_default_is_keep(self):
        components = Components(rules={"a": ["pkg/a.py"]}, selected=[])
        selector = _Selector(components)
        assert selector.keep_path("pkg/a.py") is False
        assert selector.keep_path("other.py") is True

    def test_default_exclude(self):
        components = Components(
            rules={"a": ["pkg/a.py"]}, selected=["a"], default="exclude"
        )
        selector = _Selector(components)
        assert selector.keep_path("pkg/a.py") is True
        assert selector.keep_path("other.py") is False

    def test_unselected_matching_component_excludes(self):
        components = Components(
            rules={"a": ["pkg/*.py"], "b": ["pkg/**"]}, selected=["a"]
        )
        selector = _Selector(components)
        assert selector.keep_path("pkg/x.py") is True
        assert selector.keep_path("pkg/notes/x.txt") is False

    def test_overlap_selecting_either_keeps(self):
        rules = {"a": ["pkg/core/*"], "b": ["pkg/**"]}
        path = "pkg/core/a.py"
        assert _Selector(Components(rules=rules, selected=["a"])).keep_path(path)
        assert _Selector(Components(rules=rules, selected=["b"])).keep_path(path)
        assert not _Selector(
            Components(rules=rules, selected=[], default="exclude")
        ).keep_path(path)

    def test_selected_none_selects_all_declared(self):
        selector = _Selector(Components(rules={"a": ["a/*"], "b": ["b/*"]}))
        assert selector.selected == frozenset({"a", "b"})

    def test_entry_point_default_policies(self):
        components = Components(
            rules={"a": ["x"]},
            selected=["a"],
            entry_points={"mapped": "a"},
            default="exclude",
        )
        selector = _Selector(components)
        assert selector.keep_entry_point("mapped") is True
        assert selector.keep_entry_point("unmapped") is False

        kept = Components(
            rules={"a": ["x"]},
            selected=[],
            entry_points={"mapped": "a"},
            default="keep",
        )
        kept_selector = _Selector(kept)
        assert kept_selector.keep_entry_point("mapped") is False
        assert kept_selector.keep_entry_point("unmapped") is True


class TestModuleFileCandidates:
    def test_simple(self):
        assert module_file_candidates("fancy.cli") == (
            "fancy/cli.py",
            "fancy/cli/__init__.py",
        )

    def test_top_level(self):
        assert module_file_candidates("fancy") == (
            "fancy.py",
            "fancy/__init__.py",
        )


ENTRY_POINTS_TEXT = """\
# a preamble comment

[console_scripts]
a = fancy.a:main
b = fancy.b:main

[foo.plugins]
flurb = some.module:thing

[gui_scripts]
g = fancy.gui:main
"""


class TestRewriteEntryPoints:
    def test_keep_subset_preserves_unknown_sections(self):
        result = rewrite_entry_points(ENTRY_POINTS_TEXT, {("console", "a")})
        assert "[console_scripts]\na = fancy.a:main\n" in result
        assert "b = fancy.b:main" not in result
        assert "[gui_scripts]" not in result
        assert "g = fancy.gui:main" not in result
        # Unknown section is preserved verbatim.
        assert "[foo.plugins]\nflurb = some.module:thing\n" in result
        assert "# a preamble comment" in result

    def test_keep_all_is_equivalent(self):
        kept = {
            ("console", "a"),
            ("console", "b"),
            ("gui", "g"),
        }
        assert rewrite_entry_points(ENTRY_POINTS_TEXT, kept) == ENTRY_POINTS_TEXT

    def test_keep_none_leaves_only_unknown_sections(self):
        result = rewrite_entry_points(ENTRY_POINTS_TEXT, set())
        assert "[console_scripts]" not in result
        assert "[gui_scripts]" not in result
        assert "[foo.plugins]\nflurb = some.module:thing\n" in result

    def test_keep_gui_only(self):
        result = rewrite_entry_points(ENTRY_POINTS_TEXT, {("gui", "g")})
        assert "[console_scripts]" not in result
        assert "[gui_scripts]\ng = fancy.gui:main\n" in result
        assert "[foo.plugins]" in result

    def test_no_script_sections_left(self):
        text = "[console_scripts]\na = fancy.a:main\n"
        assert rewrite_entry_points(text, set()) == ""
