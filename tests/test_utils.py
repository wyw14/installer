"""Tests for installer.utils"""

import base64
import hashlib
import textwrap
from email.message import Message
from io import BytesIO

import pytest

from installer.records import RecordEntry
from installer.scripts import InvalidScript, build_posix_wrapper
from installer.utils import (
    WheelFilename,
    canonicalize_name,
    construct_record_file,
    copyfileobj_with_hashing,
    fix_shebang,
    get_stream_length,
    parse_entrypoints,
    parse_metadata_file,
    parse_wheel_filename,
)


class TestParseMetadata:
    def test_basics(self):
        result = parse_metadata_file(
            textwrap.dedent(
                """\
            Name: package
            Version: 1.0.0
            Multi-Use-Field: 1
            Multi-Use-Field: 2
            Multi-Use-Field: 3
            """
            )
        )
        assert isinstance(result, Message)
        assert result.get("Name") == "package"
        assert result.get("version") == "1.0.0"
        assert result.get_all("MULTI-USE-FIELD") == ["1", "2", "3"]


class TestCanonicalizeDistributionName:
    @pytest.mark.parametrize(
        "string, expected",
        [
            # Noop
            (
                "package-1",
                "package-1",
            ),
            # PEP 508 canonicalization
            (
                "ABC..12",
                "abc-12",
            ),
        ],
    )
    def test_valid_cases(self, string, expected):
        got = canonicalize_name(string)
        assert expected == got, (expected, got)


class TestParseWheelFilename:
    @pytest.mark.parametrize(
        "string, expected",
        [
            # Crafted package name w/ a "complex" version and build tag
            (
                "package-1!1.0+abc.7-753-py3-none-any.whl",
                WheelFilename("package", "1!1.0+abc.7", "753", "py3-none-any"),
            ),
            # Crafted package name w/ a "complex" version and no build tag
            (
                "package-1!1.0+abc.7-py3-none-any.whl",
                WheelFilename("package", "1!1.0+abc.7", None, "py3-none-any"),
            ),
            # Use real tensorflow wheel names
            (
                "tensorflow-2.3.0-cp38-cp38-macosx_10_11_x86_64.whl",
                WheelFilename(
                    "tensorflow", "2.3.0", None, "cp38-cp38-macosx_10_11_x86_64"
                ),
            ),
            (
                "tensorflow-2.3.0-cp38-cp38-manylinux2010_x86_64.whl",
                WheelFilename(
                    "tensorflow", "2.3.0", None, "cp38-cp38-manylinux2010_x86_64"
                ),
            ),
            (
                "tensorflow-2.3.0-cp38-cp38-win_amd64.whl",
                WheelFilename("tensorflow", "2.3.0", None, "cp38-cp38-win_amd64"),
            ),
        ],
    )
    def test_valid_cases(self, string, expected):
        got = parse_wheel_filename(string)
        assert expected == got, (expected, got)

    @pytest.mark.parametrize(
        "string",
        [
            # Not ".whl"
            "pip-20.0.0-py2.py3-none-any.zip",
            # No tag
            "pip-20.0.0.whl",
            # Empty tag
            "pip-20.0.0---.whl",
        ],
    )
    def test_invalid_cases(self, string):
        with pytest.raises(ValueError):
            parse_wheel_filename(string)


class TestCopyFileObjWithHashing:
    def test_basic_functionality(self):
        data = b"input data is this"
        hash_ = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .decode("ascii")
            .rstrip("=")
        )
        size = len(data)

        with BytesIO(data) as source, BytesIO() as dest:
            result = copyfileobj_with_hashing(source, dest, hash_algorithm="sha256")
            written_data = dest.getvalue()

        assert result == (hash_, size)
        assert written_data == data


class TestGetStreamLength:
    def test_basic_functionality(self):
        data = b"input data is this"
        size = len(data)

        with BytesIO(data) as source:
            result = get_stream_length(source)

        assert result == size


class TestScript:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            pytest.param(
                b"#!python\ntest",
                b"#!/my/python\ntest",
                id="python",
            ),
            pytest.param(
                b"#!pythonw\ntest",
                b"#!/my/python\ntest",
                id="pythonw",
            ),
            pytest.param(
                b"#!python something\ntest",
                b"#!/my/python\ntest",
                id="python-with-args",
            ),
            pytest.param(
                b"#!python",
                b"#!/my/python\n",
                id="python-no-content",
            ),
        ],
    )
    def test_replace_shebang(self, data, expected):
        with BytesIO(data) as source, fix_shebang(source, "/my/python") as stream:
            result = stream.read()
        assert result == expected

    @pytest.mark.parametrize(
        "data",
        [
            b"#!py\ntest",
            b"#!something\ntest",
            b"#something\ntest",
            b"#something",
            b"something",
        ],
    )
    def test_keep_data(self, data):
        with BytesIO(data) as source, fix_shebang(source, "/my/python") as stream:
            result = stream.read()
        assert result == data

    @pytest.mark.parametrize(
        ("data", "placeholder"),
        [
            pytest.param(b"#!python\ntest", b"#!python\n", id="python"),
            pytest.param(b"#!pythonw\ntest", b"#!pythonw\n", id="pythonw"),
            pytest.param(
                b"#!python -x\ntest", b"#!python -x\n", id="python-with-old-args"
            ),
        ],
    )
    def test_replace_shebang_with_interpreter_args(self, data, placeholder):
        with (
            BytesIO(data) as source,
            fix_shebang(source, "/my/python", ["-I", "-X", "utf8"]) as stream,
        ):
            result = stream.read()

        expected = (
            build_posix_wrapper("/my/python", ["-I", "-X", "utf8"])
            + b"\n"
            + data[len(placeholder) :]
        )
        assert result == expected
        # The polyglot prefix plus the preserved body stays valid Python.
        compile(result, "<script>", "exec")

    def test_interpreter_args_preserve_body_bytes(self):
        body = b"\r\n\x00\xff arbitrary bytes\t\xc3\xa9\r\nlast line"
        data = b"#!python\n" + body
        with (
            BytesIO(data) as source,
            fix_shebang(source, "/my/python", ["-I"]) as stream,
        ):
            result = stream.read()

        assert result == build_posix_wrapper("/my/python", ["-I"]) + b"\n" + body

    def test_interpreter_args_without_trailing_newline(self):
        with (
            BytesIO(b"#!python") as source,
            fix_shebang(source, "/my/python", ["-I"]) as stream,
        ):
            result = stream.read()
        assert result == build_posix_wrapper("/my/python", ["-I"]) + b"\n"

    @pytest.mark.parametrize(
        "data",
        [
            b"#!py\ntest",
            b"#!pythonx\ntest",
            b"#!/bin/sh\ntest",
            b"#!something\ntest",
            b"#something\ntest",
            b"something",
        ],
    )
    def test_keep_other_shebangs_with_interpreter_args(self, data):
        # Only the wheel ``#!python``/``#!pythonw`` placeholders are replaced;
        # other shebangs must not be touched, even when args are configured.
        with (
            BytesIO(data) as source,
            fix_shebang(source, "/my/python", ["-I"]) as stream,
        ):
            result = stream.read()
        assert result == data

    @pytest.mark.parametrize(
        ("interpreter", "interpreter_args"),
        [
            ("/bad\x00python", ["-I"]),
            ("/bad\npython", ["-I"]),
            ("/my/python", ["a\rb"]),
            ("/my/python", ["a\x00b"]),
            ("/my/python", ["ok", "a\nb"]),
        ],
    )
    def test_rejects_forbidden_characters(self, interpreter, interpreter_args):
        with (
            pytest.raises(InvalidScript, match="forbidden character"),
            BytesIO(b"#!python\nbody") as source,
            fix_shebang(source, interpreter, interpreter_args),
        ):
            pass

    def test_rejects_before_generating_bytes(self):
        # The replacement stream is never produced when validation fails.
        source = BytesIO(b"#!python\nbody")
        with (
            pytest.raises(InvalidScript),
            fix_shebang(source, "/my/python", ["bad\narg"]),
        ):
            pass
        source.seek(0)
        assert source.read() == b"#!python\nbody"


class TestConstructRecord:
    def test_construct(self):
        raw_records = [
            ("a.py", "", ""),
            ("a.py", "", "3144"),
            ("a.py", "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI", ""),
            ("a.py", "sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI", "3144"),
        ]
        records = [
            ("purelib", RecordEntry.from_elements(*elements))
            for elements in raw_records
        ]

        assert construct_record_file(records).read() == (
            b"a.py,,\n"
            b"a.py,,3144\n"
            b"a.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,\n"
            b"a.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144\n"
        )


class TestParseEntryPoints:
    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            pytest.param("", [], id="empty"),
            pytest.param(
                """
                    [foo]
                    foo = foo.bar
                """,
                [],
                id="unrelated",
            ),
            pytest.param(
                """
                    [console_scripts]
                    package = package.__main__:package
                """,
                [
                    ("package", "package.__main__", "package", "console"),
                ],
                id="cli",
            ),
            pytest.param(
                """
                    [gui_scripts]
                    package = package.__main__:package
                """,
                [
                    ("package", "package.__main__", "package", "gui"),
                ],
                id="gui",
            ),
            pytest.param(
                """
                    [console_scripts]
                    magic-cli = magic.cli:main

                    [gui_scripts]
                    magic-gui = magic.gui:main
                """,
                [
                    ("magic-cli", "magic.cli", "main", "console"),
                    ("magic-gui", "magic.gui", "main", "gui"),
                ],
                id="cli-and-gui",
            ),
        ],
    )
    def test_valid(self, script, expected):
        iterable = parse_entrypoints(textwrap.dedent(script))
        assert list(iterable) == expected, expected
