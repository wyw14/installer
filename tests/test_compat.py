"""Tests for cross-target compatibility checks."""

import hashlib
import textwrap
import zipfile
from io import BytesIO
from unittest import mock

import pytest

from installer import Target, check_compatibility, install_for_target
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import (
    InvalidRequiresPython,
    InvalidTarget,
    InvalidWheelSource,
    InvalidWheelTags,
    NoCompatibleTags,
    UnsupportedPythonVersion,
    WheelTagMismatch,
)
from installer.sources import WheelSource


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def hash_and_size(data):
    return hashlib.sha256(data).hexdigest(), len(data)


@pytest.fixture
def mock_destination():
    retval = mock.Mock()

    def custom_write_file(scheme, path, stream, is_executable):
        assert isinstance(stream, BytesIO)
        return (path, scheme, 0)

    def custom_write_script(name, module, attr, section):
        return (name, module, attr, section)

    retval.write_file.side_effect = custom_write_file
    retval.write_script.side_effect = custom_write_script
    return retval


def wheel_metadata(
    *tags,
    root_is_purelib="true",
    wheel_version="1.0",
    requires_python=None,
):
    wheel_lines = ["Generator: magic (1.0.0)", f"Root-Is-Purelib: {root_is_purelib}"]
    if wheel_version is not None:
        wheel_lines.insert(0, f"Wheel-Version: {wheel_version}")
    wheel_lines.extend(f"Tag: {tag}" for tag in tags)
    metadata_lines = [
        "Metadata-Version: 2.1",
        "Name: fancy",
        "Version: 1.0.0",
        "Summary: A fancy package",
    ]
    if requires_python is not None:
        metadata_lines.append(f"Requires-Python: {requires_python}")
    return {
        "WHEEL": "\n".join(wheel_lines) + "\n",
        "METADATA": "\n".join(metadata_lines) + "\n",
    }


class FakeWheelSource(WheelSource):
    def __init__(
        self,
        *,
        wheel_tag,
        tags,
        root_is_purelib="true",
        wheel_version="1.0",
        requires_python=None,
        entry_points=False,
    ):
        super().__init__("fancy", "1.0.0")
        self._wheel_tag = wheel_tag
        self.regular_files = {
            "fancy/__init__.py": b"# put me in purelib\n",
        }
        self.dist_info_files = wheel_metadata(
            *tags,
            root_is_purelib=root_is_purelib,
            wheel_version=wheel_version,
            requires_python=requires_python,
        )
        if entry_points:
            self.dist_info_files["entry_points.txt"] = textwrap.dedent(
                """\
                [console_scripts]
                fancy = fancy:main
                """
            )

    @property
    def wheel_tag(self):
        if isinstance(self._wheel_tag, Exception):
            raise self._wheel_tag
        return self._wheel_tag

    @property
    def dist_info_filenames(self):
        return list(self.dist_info_files)

    def read_dist_info(self, filename):
        return self.dist_info_files[filename]

    def validate_record(self):
        return

    def get_contents(self):
        for file, content in sorted(self.regular_files.items()):
            hashed, size = hash_and_size(content)
            with BytesIO(content) as stream:
                yield (file, f"sha256={hashed}", str(size)), stream, False

        for file, text in sorted(self.dist_info_files.items()):
            content = text.encode("utf-8")
            hashed, size = hash_and_size(content)
            record = (
                self.dist_info_dir + "/" + file,
                f"sha256={hashed}",
                str(size),
            )
            with BytesIO(content) as stream:
                yield record, stream, False


def assert_no_writes(destination):
    destination.write_file.assert_not_called()
    destination.write_script.assert_not_called()
    destination.finalize_installation.assert_not_called()


LINUX_TAGS = [
    "cp311-cp311-manylinux_2_17_x86_64",
    "cp311-abi3-manylinux_2_17_x86_64",
    "py311-none-any",
    "py3-none-any",
]
WINDOWS_TAGS = [
    "cp311-cp311-win_amd64",
    "cp311-abi3-win_amd64",
    "py311-none-any",
    "py3-none-any",
]


def pure_source(**kwargs):
    kwargs.setdefault("wheel_tag", "py2.py3-none-any")
    kwargs.setdefault("tags", ("py2-none-any", "py3-none-any"))
    return FakeWheelSource(**kwargs)


def build_real_wheel(path, *, requires_python=None):
    files = {
        "fancy/__init__.py": b"def main():\n    print('fancy')\n",
        "fancy-1.0.0.dist-info/top_level.txt": b"fancy\n",
        "fancy-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nfancy = fancy:main\n"
        ),
    }
    wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: magic (1.0.0)\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    metadata = "Metadata-Version: 2.1\nName: fancy\nVersion: 1.0.0\n"
    if requires_python is not None:
        metadata += f"Requires-Python: {requires_python}\n"
    files["fancy-1.0.0.dist-info/WHEEL"] = wheel.encode()
    files["fancy-1.0.0.dist-info/METADATA"] = metadata.encode()
    files["fancy-1.0.0.dist-info/RECORD"] = b"fancy-1.0.0.dist-info/RECORD,,\n"

    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


# --------------------------------------------------------------------------------------
# Tag expansion and filename/WHEEL consistency
# --------------------------------------------------------------------------------------
class TestWheelTags:
    def test_py2_py3_none_any_expansion(self):
        source = pure_source()
        target = Target(
            tags=["py3-none-any", "py2-none-any"],
            python_version="3.11.4",
            launcher_kind="posix",
        )
        assert check_compatibility(source, target) == "py3-none-any"

    def test_py2_py3_expansion_picks_first_target_in_order(self):
        source = pure_source()
        target = Target(
            tags=["py2-none-any", "py3-none-any"],
            python_version="2.7.18",
            launcher_kind="posix",
        )
        assert check_compatibility(source, target) == "py2-none-any"

    def test_single_wheel_tag_field_with_compressed_expansion(self):
        source = pure_source(tags=("py2.py3-none-any",))
        target = Target(["py3-none-any"], "3.11.4", "posix")
        assert check_compatibility(source, target) == "py3-none-any"

    def test_multiple_abi_and_platform_tags(self):
        compressed = "cp310-cp310.abi3-manylinux1_x86_64.manylinux_2_17_x86_64"
        expanded = (
            "cp310-cp310-manylinux1_x86_64",
            "cp310-cp310-manylinux_2_17_x86_64",
            "cp310-abi3-manylinux1_x86_64",
            "cp310-abi3-manylinux_2_17_x86_64",
        )
        source = FakeWheelSource(wheel_tag=compressed, tags=expanded)
        target = Target(
            tags=["cp310-abi3-manylinux_2_17_x86_64"],
            python_version="3.10.8",
            launcher_kind="posix",
        )
        assert check_compatibility(source, target) == "cp310-abi3-manylinux_2_17_x86_64"

    def test_compressed_target_entries_are_expanded(self):
        source = pure_source()
        target = Target(["py2.py3-none-any"], "3.11.4", "posix")
        assert check_compatibility(source, target) == "py2-none-any"

    def test_duplicate_tag_fields_are_deduplicated(self):
        source = pure_source(
            tags=(
                "py2-none-any",
                "py3-none-any",
                "py2-none-any",
                "py2.py3-none-any",
            )
        )
        target = Target(["py3-none-any"], "3.11.4", "posix")
        assert check_compatibility(source, target) == "py3-none-any"

    def test_only_lower_priority_target_tag_matches(self):
        source = pure_source()
        target = Target(
            tags=[
                "cp311-cp311-win_amd64",
                "cp311-abi3-win_amd64",
                "py311-none-any",
                "py3-none-any",
            ],
            python_version="3.11.4",
            launcher_kind="win-amd64",
        )
        assert check_compatibility(source, target) == "py3-none-any"

    @pytest.mark.parametrize(
        "bad_tag",
        [
            "py3-none",  # missing platform
            "py3-none-",  # empty platform
            "-none-any",  # empty interpreter
            "py3..none-any",  # empty compressed segment
            "py 3-none-any",  # illegal character
        ],
    )
    def test_invalid_filename_tag_syntax(self, mock_destination, bad_tag):
        source = pure_source(wheel_tag=bad_tag, tags=("py3-none-any",))
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidWheelTags):
            check_compatibility(source, target)
        with pytest.raises(InvalidWheelTags):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    @pytest.mark.parametrize(
        "bad_tag",
        ["py3-none", "py3-none-", "py3..none-any", "py3-none-any/bad"],
    )
    def test_invalid_wheel_metadata_tag_syntax(self, mock_destination, bad_tag):
        source = pure_source(tags=(bad_tag,))
        target = Target(["py2-none-any", "py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidWheelTags):
            check_compatibility(source, target)
        with pytest.raises(InvalidWheelTags):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_missing_tag_fields(self, mock_destination):
        source = FakeWheelSource(
            wheel_tag="py3-none-any",
            tags=(),
        )
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidWheelTags):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_filename_and_wheel_tags_mismatch(self, mock_destination):
        source = pure_source(tags=("py2-none-any", "py3-none-any", "py310-none-any"))
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(WheelTagMismatch):
            check_compatibility(source, target)
        with pytest.raises(WheelTagMismatch):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_no_intersection(self, mock_destination):
        compressed = "cp310-cp310-manylinux_2_17_x86_64"
        source = FakeWheelSource(wheel_tag=compressed, tags=(compressed,))
        target = Target(WINDOWS_TAGS, "3.11.4", "win-amd64")
        with pytest.raises(NoCompatibleTags):
            check_compatibility(source, target)
        with pytest.raises(NoCompatibleTags):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_purelib_true_does_not_bypass_platform_mismatch(self, mock_destination):
        # A wheel that claims Root-Is-Purelib: true while carrying a platform
        # specific tag must not be installable on an incompatible target.
        compressed = "cp311-cp311-manylinux_2_17_x86_64"
        source = FakeWheelSource(
            wheel_tag=compressed, tags=(compressed,), root_is_purelib="true"
        )
        target = Target(WINDOWS_TAGS, "3.11.4", "win-amd64")
        with pytest.raises(NoCompatibleTags):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_unsupported_wheel_version(self, mock_destination):
        source = pure_source(wheel_version="2.0")
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidWheelSource):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_missing_wheel_version(self, mock_destination):
        source = pure_source(wheel_version=None)
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidWheelSource):
            check_compatibility(source, target)
        with pytest.raises(InvalidWheelSource):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_base_wheel_source_does_not_provide_wheel_tag(self):
        source = WheelSource("fancy", "1.0.0")
        with pytest.raises(NotImplementedError):
            _ = source.wheel_tag


# --------------------------------------------------------------------------------------
# Requires-Python
# --------------------------------------------------------------------------------------
class TestRequiresPython:
    def test_target_satisfies_but_host_cannot(self, mock_destination):
        # No host Python satisfies '>=3.99'; the explicit target 3.100.4 does.
        source = pure_source(requires_python=">=3.99")
        target = Target(["py3-none-any"], "3.100.4", "posix")
        assert (
            install_for_target(source, mock_destination, {}, target) == "py3-none-any"
        )
        mock_destination.finalize_installation.assert_called_once()

    def test_host_would_satisfy_but_target_does_not(self, mock_destination):
        # Every supported host (3.x) satisfies '>=3.0'; target 2.7.18 fails.
        source = pure_source(requires_python=">=3.0")
        target = Target(["py2-none-any"], "2.7.18", "posix")
        with pytest.raises(UnsupportedPythonVersion):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    @pytest.mark.parametrize(
        ("specifier", "version", "expected"),
        [
            (">=3.10,<3.13", "3.10.0", True),
            (">=3.10,<3.13", "3.12.5", True),
            (">=3.10,<3.13", "3.13.0", False),
            (">= 3.10 , < 4", "3.11.4", True),
            (">=3.10,!=3.11.0", "3.11.0", False),
            (">=3.10,!=3.11.0", "3.11.1", True),
            ("~=3.11.2", "3.11.9", True),
            ("~=3.11.2", "3.12.0", False),
            ("~=3.11", "3.12.0", True),
            ("==3.11.*", "3.11.4", True),
            ("==3.11.*", "3.12.0", False),
            ("!=3.0.*", "3.0.8", False),
            ("!=3.0.*", "3.1.0", True),
            ("==3.11", "3.11.0", True),
            ("==3.11", "3.11.4", False),
            (">1!3.11", "1!3.11.4", True),
            ("<=3.11.4", "3.11.4", True),
            ("<=3.11.4", "3.11.5", False),
            (">=3.13.0a1", "3.13.0a2", True),
            (">=3.13.0a1", "3.13.0.dev1", False),
        ],
    )
    def test_specifier_combination_cases(
        self, mock_destination, specifier, version, expected
    ):
        source = pure_source(requires_python=specifier)
        launcher = "posix"
        tags = LINUX_TAGS
        target = Target(tags, version, launcher)
        if expected:
            install_for_target(source, mock_destination, {}, target)
            mock_destination.finalize_installation.assert_called_once()
        else:
            with pytest.raises(UnsupportedPythonVersion):
                install_for_target(source, mock_destination, {}, target)
            assert_no_writes(mock_destination)

    @pytest.mark.parametrize(
        ("version", "matches"),
        [
            ("3.12.0rc1", False),  # rc1 precedes the 3.12.0 final release
            ("3.12.0", True),
        ],
    )
    def test_explicit_prerelease_target(self, mock_destination, version, matches):
        source = pure_source(requires_python=">=3.12")
        target = Target(["py3-none-any"], version, "posix")
        if matches:
            check_compatibility(source, target)
        else:
            with pytest.raises(UnsupportedPythonVersion):
                check_compatibility(source, target)

    def test_prerelease_target_satisfies_lower_bound_within_its_release(self):
        source = pure_source(requires_python=">=3.11")
        target = Target(["py3-none-any"], "3.12.0rc1", "posix")
        assert check_compatibility(source, target) == "py3-none-any"

    def test_missing_patch_in_target_version(self, mock_destination):
        source = pure_source(requires_python=">=3.10")
        target = Target(["py3-none-any"], "3.11", "posix")
        with pytest.raises(InvalidTarget, match="patch"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_missing_patch_checked_without_requires_python(self):
        source = pure_source()
        target = Target(["py3-none-any"], "3", "posix")
        with pytest.raises(InvalidTarget, match="patch"):
            check_compatibility(source, target)

    def test_invalid_target_version_syntax(self, mock_destination):
        source = pure_source()
        target = Target(["py3-none-any"], "3.11.x", "posix")
        with pytest.raises(InvalidTarget):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    @pytest.mark.parametrize(
        "specifier",
        [
            "3.7",  # missing operator
            "~=3",  # compatible release needs V.N
            ">=3.7.*",  # prefix only allowed with == / !=
            "=>3.7",  # unknown operator
            ">=3.7,,<4",  # empty clause
            "===3.11",  # arbitrary equality unsupported
            "==3.x.*",  # invalid version
            "==3.11a1.*",  # prerelease not allowed in prefix specifier
            ">=  ",  # missing version
        ],
    )
    def test_invalid_requires_python_constraint(self, mock_destination, specifier):
        source = pure_source(requires_python=specifier)
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidRequiresPython):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_empty_requires_python(self, mock_destination):
        source = pure_source(requires_python="   ")
        target = Target(["py3-none-any"], "3.11.4", "posix")
        with pytest.raises(InvalidRequiresPython):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_version_post_dev_local_and_aliases(self):
        from installer._compat import _parse_version

        assert _parse_version("3.11.4.post1") > _parse_version("3.11.4")
        assert _parse_version("3.11.4-1") > _parse_version("3.11.4")
        assert _parse_version("3.11.4.dev3") < _parse_version("3.11.4")
        assert _parse_version("3.12.0alpha1") == _parse_version("3.12.0a1")
        assert _parse_version("3.12.0beta1") == _parse_version("3.12.0b1")
        assert _parse_version("3.12.0c1") == _parse_version("3.12.0rc1")
        assert _parse_version("3.12.0preview1") == _parse_version("3.12.0rc1")
        assert _parse_version("3.11.4+local.1") != _parse_version("3.11.4")
        assert _parse_version("3.11.4+local.1") == _parse_version("3.11.4+LOCAL.1")
        assert _parse_version("3.11.4") != "not a version"
        assert (
            len(
                {
                    _parse_version("3.11.4"),
                    _parse_version("3.11.4"),
                    _parse_version("3.11.5"),
                }
            )
            == 2
        )


# --------------------------------------------------------------------------------------
# Target description and launcher kind compatibility
# --------------------------------------------------------------------------------------
class TestTargetValidation:
    @pytest.mark.parametrize(
        "tags",
        [
            (),  # empty
            "py3-none-any",  # a single string instead of a sequence
        ],
    )
    def test_invalid_tag_list(self, mock_destination, tags):
        source = pure_source()
        target = Target(tags, "3.11.4", "posix")
        with pytest.raises(InvalidTarget):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_non_string_tag_entry(self, mock_destination):
        target = Target(["py3-none-any", 42], "3.11.4", "posix")
        with pytest.raises(InvalidTarget):
            check_compatibility(pure_source(), target)

    def test_invalid_target_tag_syntax(self, mock_destination):
        source = pure_source()
        target = Target(["py3-none"], "3.11.4", "posix")
        with pytest.raises(InvalidTarget):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_posix_launcher_for_windows_target_rejected(self, mock_destination):
        # Even a pure-Python universal wheel must not get a posix launcher set
        # when the target is Windows.
        source = pure_source()
        target = Target(WINDOWS_TAGS, "3.11.4", "posix")
        with pytest.raises(InvalidTarget, match="posix"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_pe_launcher_for_posix_target_rejected(self, mock_destination):
        # Even a pure-Python universal wheel must not get a PE launcher when the
        # target platform is Linux.
        source = pure_source()
        target = Target(LINUX_TAGS, "3.11.4", "win-amd64")
        with pytest.raises(InvalidTarget, match="win-amd64"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_launcher_architecture_mismatch_rejected(self, mock_destination):
        tags = [tag.replace("win_amd64", "win_arm64") for tag in WINDOWS_TAGS]
        source = pure_source()
        target = Target(tags, "3.11.4", "win-amd64")
        with pytest.raises(InvalidTarget, match="win_amd64"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_mixed_platform_target_rejected(self, mock_destination):
        source = pure_source()
        target = Target(
            ["cp311-cp311-win_amd64", "cp311-cp311-manylinux_2_17_x86_64"],
            "3.11.4",
            "win-amd64",
        )
        with pytest.raises(InvalidTarget, match="mix"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_unknown_launcher_kind_rejected(self, mock_destination):
        source = pure_source()
        target = Target(LINUX_TAGS, "3.11.4", "amiga")
        with pytest.raises(InvalidTarget, match="launcher"):
            install_for_target(source, mock_destination, {}, target)
        assert_no_writes(mock_destination)

    def test_posix_target_with_posix_launcher(self):
        source = pure_source()
        target = Target(LINUX_TAGS, "3.11.4", "posix")
        assert check_compatibility(source, target) == "py3-none-any"

    def test_windows_target_with_matching_pe_launcher(self):
        source = pure_source()
        target = Target(WINDOWS_TAGS, "3.11.4", "win-amd64")
        assert check_compatibility(source, target) == "py3-none-any"

    def test_generic_only_target_allows_only_posix(self):
        source = pure_source()
        assert (
            check_compatibility(source, Target(["py3-none-any"], "3.11.4", "posix"))
            == "py3-none-any"
        )
        with pytest.raises(InvalidTarget):
            check_compatibility(source, Target(["py3-none-any"], "3.11.4", "win-amd64"))

    def test_destination_launcher_kind_must_match_target(self, tmp_path):
        source = pure_source()
        target = Target(LINUX_TAGS, "3.11.4", "posix")
        destination = SchemeDictionaryDestination(
            scheme_dict={
                scheme: str(tmp_path / scheme)
                for scheme in (
                    "purelib",
                    "platlib",
                    "headers",
                    "scripts",
                    "data",
                )
            },
            interpreter="/usr/bin/python3",
            script_kind="win-amd64",
        )
        with pytest.raises(InvalidTarget, match="does not match"):
            install_for_target(source, destination, {}, target)
        assert not any(tmp_path.rglob("*"))


# --------------------------------------------------------------------------------------
# install_for_target end-to-end behaviour
# --------------------------------------------------------------------------------------
class TestInstallForTarget:
    def test_writes_only_after_successful_check(self, mock_destination):
        source = pure_source(entry_points=True)
        target = Target(LINUX_TAGS, "3.11.4", "posix")
        matched = install_for_target(
            source,
            mock_destination,
            {"INSTALLER": b"installer"},
            target,
        )
        assert matched == "py3-none-any"
        mock_destination.write_script.assert_called_once_with(
            name="fancy",
            module="fancy",
            attr="main",
            section="console",
        )
        mock_destination.write_file.assert_called()
        mock_destination.finalize_installation.assert_called_once()

    def test_real_wheel_end_to_end(self, tmp_path):
        from installer.sources import WheelFile

        wheel_path = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        build_real_wheel(wheel_path, requires_python=">=3.10")

        schemes = {
            scheme: str(tmp_path / scheme)
            for scheme in ("purelib", "platlib", "headers", "scripts", "data")
        }
        destination = SchemeDictionaryDestination(
            scheme_dict=schemes,
            interpreter="/usr/bin/python3.11",
            script_kind="posix",
        )
        target = Target(LINUX_TAGS, "3.11.4", "posix")
        with WheelFile.open(wheel_path) as source:
            matched = install_for_target(source, destination, {}, target)

        assert matched == "py3-none-any"
        assert (tmp_path / "purelib" / "fancy" / "__init__.py").exists()
        assert (tmp_path / "scripts" / "fancy").exists()
        assert (tmp_path / "purelib" / "fancy-1.0.0.dist-info" / "RECORD").exists()

    def test_real_wheel_check_failure_writes_nothing(self, tmp_path):
        from installer.sources import WheelFile

        wheel_path = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        build_real_wheel(wheel_path, requires_python=">=3.10")

        schemes = {
            scheme: str(tmp_path / "root" / scheme)
            for scheme in ("purelib", "platlib", "headers", "scripts", "data")
        }
        destination = SchemeDictionaryDestination(
            scheme_dict=schemes,
            interpreter="/usr/bin/python3.11",
            script_kind="posix",
        )
        target = Target(LINUX_TAGS, "3.9.0", "posix")
        with (
            WheelFile.open(wheel_path) as source,
            pytest.raises(UnsupportedPythonVersion),
        ):
            install_for_target(source, destination, {}, target)

        root = tmp_path / "root"
        assert not root.exists() or not any(root.rglob("*"))
