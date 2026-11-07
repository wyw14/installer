import hashlib
import textwrap
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

from installer import Components, install
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import InvalidComponents, InvalidWheelSource
from installer.records import RecordEntry, parse_record_file
from installer.sources import WheelSource
from installer.utils import SCHEME_NAMES


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def hash_and_size(data):
    return hashlib.sha256(data).hexdigest(), len(data)


def make_mock_destination():
    retval = mock.Mock()

    # A hacky approach to making sure we got the right objects going in.
    def custom_write_file(scheme, path, stream, is_executable):
        assert isinstance(stream, BytesIO)
        return (path, scheme, 0)

    def custom_write_script(name, module, attr, section):
        return (name, module, attr, section)

    retval.write_file.side_effect = custom_write_file
    retval.write_script.side_effect = custom_write_script

    return retval


@pytest.fixture
def mock_destination():
    return make_mock_destination()


def written_paths(mock_dest):
    return [call.kwargs["path"] for call in mock_dest.write_file.call_args_list]


def written_script_names(mock_dest):
    return [call.kwargs["name"] for call in mock_dest.write_script.call_args_list]


def final_records(mock_dest):
    assert mock_dest.finalize_installation.called
    return mock_dest.finalize_installation.call_args.kwargs["records"]


def make_component_wheel(
    *, with_entry_points=True, unicode_entry=False, platlib_module=False
):
    regular_files = {
        "fancy/__init__.py": b"""\
            # fancy package
        """,
        "fancy/cli.py": b"""\
            def main():
                return "cli"
        """,
        "fancy/a.py": b"""\
            def main():
                return "a"
        """,
        "fancy/b.py": b"""\
            def main():
                return "b"
        """,
        "fancy/gui.py": b"""\
            def main():
                return "g"
        """,
        "fancy/data/notes.txt": b"""\
            notes
        """,
        "fancy/数据.txt": textwrap.dedent(
            """\
            数据
            """
        ).encode("utf-8"),
        "fancy-1.0.0.data/data/fancy/data.bin": b"""\
            data
        """,
        "fancy-1.0.0.data/scripts/fancy-tool": b"""\
            #!/bin/sh
            echo hi
        """,
    }
    if platlib_module:
        regular_files["fancy-1.0.0.data/platlib/pkg/x.py"] = b"""\
            def main():
                return "x"
        """
    if unicode_entry:
        regular_files["ünïcode/__init__.py"] = b"""\
            # unicode-named package
        """

    dist_info_files = {
        "WHEEL": b"""\
            Wheel-Version: 1.0
            Generator: magic (1.0.0)
            Root-Is-Purelib: true
            Tag: py3-none-any
        """,
        "METADATA": b"""\
            Metadata-Version: 2.1
            Name: fancy
            Version: 1.0.0
            Requires-Dist: some-dep>=1.0
        """,
        "top_level.txt": b"""\
            fancy
        """,
    }
    if with_entry_points:
        entry_point_lines = [
            "[console_scripts]",
            "fancy = fancy.cli:main",
            "a = fancy.a:main",
            "b = fancy.b:main",
        ]
        if unicode_entry:
            entry_point_lines.append("u = ünïcode:main")
        if platlib_module:
            entry_point_lines.append("x = pkg.x:main")
        entry_point_lines += [
            "",
            "[gui_scripts]",
            "g = fancy.gui:main",
            "",
            "[foo.plugins]",
            "flurb = some.module:thing",
            "",
        ]
        dist_info_files["entry_points.txt"] = "\n".join(entry_point_lines).encode(
            "utf-8"
        )

    return FakeWheelSource(
        distribution="fancy",
        version="1.0.0",
        regular_files=regular_files,
        dist_info_files=dist_info_files,
    )


class FakeWheelSource(WheelSource):
    def __init__(self, *, distribution, version, regular_files, dist_info_files):
        super().__init__(distribution, version)

        self.dist_info_files = {
            file: textwrap.dedent(content.decode("utf-8"))
            for file, content in dist_info_files.items()
        }
        self.regular_files = {
            file: textwrap.dedent(content.decode("utf-8")).encode("utf-8")
            for file, content in regular_files.items()
        }

        # Compute RECORD file.
        _records = [record for record, _, _ in self.get_contents()]
        self.dist_info_files["RECORD"] = "\n".join(
            sorted(
                ",".join([file, "sha256=" + hash_, str(size)])
                for file, hash_, size in _records
            )
        )

    @property
    def dist_info_filenames(self):
        return list(self.dist_info_files)

    def read_dist_info(self, filename):
        return self.dist_info_files[filename]

    def validate_record(self) -> None:
        # Skip validation since the logic is different.
        return

    def get_contents(self):
        # Sort for deterministic behaviour for Python versions that do not preserve
        # insertion order for dictionaries.
        for file, content in sorted(self.regular_files.items()):
            hashed, size = hash_and_size(content)
            record = (file, f"sha256={hashed}", str(size))
            with BytesIO(content) as stream:
                yield record, stream, False

        # Sort for deterministic behaviour for Python versions that do not preserve
        # insertion order for dictionaries.
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


# --------------------------------------------------------------------------------------
# Actual Tests
# --------------------------------------------------------------------------------------
class TestInstall:
    def test_calls_destination_correctly(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    def main():
                        print("I'm a fancy package")
                """,
                "fancy/__main__.py": b"""\
                    if __name__ == "__main__":
                        from . import main
                        main()
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        install(
            source=source,
            destination=mock_destination,
            additional_metadata={
                "fun_file.txt": b"this should be in dist-info!",
            },
        )

        mock_destination.assert_has_calls(
            [
                mock.call.write_script(
                    name="fancy",
                    module="fancy",
                    attr="main",
                    section="console",
                ),
                mock.call.write_script(
                    name="fancy-gui",
                    module="fancy",
                    attr="main",
                    section="gui",
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__init__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__main__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/METADATA",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/WHEEL",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/entry_points.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/top_level.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/fun_file.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.finalize_installation(
                    scheme="purelib",
                    record_file_path="fancy-1.0.0.dist-info/RECORD",
                    records=[
                        ("scripts", ("fancy", "fancy", "main", "console")),
                        ("scripts", ("fancy-gui", "fancy", "main", "gui")),
                        ("purelib", ("fancy/__init__.py", "purelib", 0)),
                        ("purelib", ("fancy/__main__.py", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/METADATA", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/WHEEL", "purelib", 0)),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/entry_points.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/top_level.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/fun_file.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            RecordEntry("fancy-1.0.0.dist-info/RECORD", None, None),
                        ),
                    ],
                ),
            ]
        )

    def test_no_entrypoints_is_ok(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    def main():
                        print("I'm a fancy package")
                """,
                "fancy/__main__.py": b"""\
                    if __name__ == "__main__":
                        from . import main
                        main()
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        install(
            source=source,
            destination=mock_destination,
            additional_metadata={
                "fun_file.txt": b"this should be in dist-info!",
            },
        )

        mock_destination.assert_has_calls(
            [
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__init__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__main__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/METADATA",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/WHEEL",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/top_level.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/fun_file.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.finalize_installation(
                    scheme="purelib",
                    record_file_path="fancy-1.0.0.dist-info/RECORD",
                    records=[
                        ("purelib", ("fancy/__init__.py", "purelib", 0)),
                        ("purelib", ("fancy/__main__.py", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/METADATA", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/WHEEL", "purelib", 0)),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/top_level.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/fun_file.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            RecordEntry("fancy-1.0.0.dist-info/RECORD", None, None),
                        ),
                    ],
                ),
            ]
        )

    def test_handles_platlib(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    def main():
                        print("I'm a fancy package")
                """,
                "fancy/__main__.py": b"""\
                    if __name__ == "__main__":
                        from . import main
                        main()
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: false
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        install(
            source=source,
            destination=mock_destination,
            additional_metadata={
                "fun_file.txt": b"this should be in dist-info!",
            },
        )

        mock_destination.assert_has_calls(
            [
                mock.call.write_script(
                    name="fancy",
                    module="fancy",
                    attr="main",
                    section="console",
                ),
                mock.call.write_script(
                    name="fancy-gui",
                    module="fancy",
                    attr="main",
                    section="gui",
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy/__init__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy/__main__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy-1.0.0.dist-info/METADATA",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy-1.0.0.dist-info/WHEEL",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy-1.0.0.dist-info/entry_points.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy-1.0.0.dist-info/top_level.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy-1.0.0.dist-info/fun_file.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.finalize_installation(
                    scheme="platlib",
                    record_file_path="fancy-1.0.0.dist-info/RECORD",
                    records=[
                        ("scripts", ("fancy", "fancy", "main", "console")),
                        ("scripts", ("fancy-gui", "fancy", "main", "gui")),
                        ("platlib", ("fancy/__init__.py", "platlib", 0)),
                        ("platlib", ("fancy/__main__.py", "platlib", 0)),
                        ("platlib", ("fancy-1.0.0.dist-info/METADATA", "platlib", 0)),
                        ("platlib", ("fancy-1.0.0.dist-info/WHEEL", "platlib", 0)),
                        (
                            "platlib",
                            ("fancy-1.0.0.dist-info/entry_points.txt", "platlib", 0),
                        ),
                        (
                            "platlib",
                            ("fancy-1.0.0.dist-info/top_level.txt", "platlib", 0),
                        ),
                        (
                            "platlib",
                            ("fancy-1.0.0.dist-info/fun_file.txt", "platlib", 0),
                        ),
                        (
                            "platlib",
                            RecordEntry("fancy-1.0.0.dist-info/RECORD", None, None),
                        ),
                    ],
                ),
            ]
        )

    def test_accepts_newer_minor_wheel_versions(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    def main():
                        print("I'm a fancy package")
                """,
                "fancy/__main__.py": b"""\
                    if __name__ == "__main__":
                        from . import main
                        main()
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.1
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        install(
            source=source,
            destination=mock_destination,
            additional_metadata={
                "fun_file.txt": b"this should be in dist-info!",
            },
        )

        # no assertions necessary, since we want to make sure this test didn't
        # raises errors.
        assert True

    def test_rejects_newer_major_wheel_versions(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    def main():
                        print("I'm a fancy package")
                """,
                "fancy/__main__.py": b"""\
                    if __name__ == "__main__":
                        from . import main
                        main()
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 2.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        with pytest.raises(InvalidWheelSource) as ctx:
            install(
                source=source,
                destination=mock_destination,
                additional_metadata={
                    "fun_file.txt": b"this should be in dist-info!",
                },
            )

        assert "Incompatible Wheel-Version" in str(ctx.value)

    def test_handles_data_properly(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    # put me in purelib
                """,
                "fancy-1.0.0.data/purelib/fancy/purelib.py": b"""\
                    # put me in purelib
                """,
                "fancy-1.0.0.data/platlib/fancy/platlib.py": b"""\
                    # put me in platlib
                """,
                "fancy-1.0.0.data/scripts/fancy/scripts.py": b"""\
                    # put me in scripts
                """,
                "fancy-1.0.0.data/headers/fancy/headers.py": b"""\
                    # put me in headers
                """,
                "fancy-1.0.0.data/data/fancy/data.py": b"""\
                    # put me in data
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        install(
            source=source,
            destination=mock_destination,
            additional_metadata={},
        )

        mock_destination.assert_has_calls(
            [
                mock.call.write_script(
                    name="fancy",
                    module="fancy",
                    attr="main",
                    section="console",
                ),
                mock.call.write_script(
                    name="fancy-gui",
                    module="fancy",
                    attr="main",
                    section="gui",
                ),
                mock.call.write_file(
                    scheme="data",
                    path="fancy/data.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="headers",
                    path="fancy/headers.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="platlib",
                    path="fancy/platlib.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/purelib.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="scripts",
                    path="fancy/scripts.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__init__.py",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/METADATA",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/WHEEL",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/entry_points.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/top_level.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
                mock.call.finalize_installation(
                    scheme="purelib",
                    record_file_path="fancy-1.0.0.dist-info/RECORD",
                    records=[
                        ("scripts", ("fancy", "fancy", "main", "console")),
                        ("scripts", ("fancy-gui", "fancy", "main", "gui")),
                        ("data", ("fancy/data.py", "data", 0)),
                        ("headers", ("fancy/headers.py", "headers", 0)),
                        ("platlib", ("fancy/platlib.py", "platlib", 0)),
                        ("purelib", ("fancy/purelib.py", "purelib", 0)),
                        ("scripts", ("fancy/scripts.py", "scripts", 0)),
                        ("purelib", ("fancy/__init__.py", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/METADATA", "purelib", 0)),
                        ("purelib", ("fancy-1.0.0.dist-info/WHEEL", "purelib", 0)),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/entry_points.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            ("fancy-1.0.0.dist-info/top_level.txt", "purelib", 0),
                        ),
                        (
                            "purelib",
                            RecordEntry("fancy-1.0.0.dist-info/RECORD", None, None),
                        ),
                    ],
                ),
            ]
        )

    def test_errors_out_when_given_invalid_scheme_in_data(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    # put me in purelib
                """,
                "fancy-1.0.0.data/purelib/fancy/purelib.py": b"""\
                    # put me in purelib
                """,
                "fancy-1.0.0.data/invalid/fancy/invalid.py": b"""\
                    # i am invalid
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main

                    [gui_scripts]
                    fancy-gui = fancy:main
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )

        with pytest.raises(InvalidWheelSource) as ctx:
            install(
                source=source,
                destination=mock_destination,
                additional_metadata={},
            )

        assert "fancy-1.0.0.data/invalid/fancy/invalid.py" in str(ctx.value)

    def test_ensure_non_executable_for_additional_metadata(self, mock_destination):
        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                "fancy/__init__.py": b"""\
                    # put me in purelib
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                    Summary: A fancy package
                    Author: Agendaless Consulting
                    Author-email: nobody@example.com
                    License: MIT
                    Keywords: fancy amazing
                    Platform: UNKNOWN
                    Classifier: Intended Audience :: Developers
                """,
            },
        )
        all_contents = list(source.get_contents())
        source.get_contents = lambda: (
            (*contents, True) for (*contents, _) in all_contents
        )
        install(
            source=source,
            destination=mock_destination,
            additional_metadata={
                "fun_file.txt": b"this should be in dist-info!",
            },
        )

        mock_destination.assert_has_calls(
            [
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy/__init__.py",
                    stream=mock.ANY,
                    is_executable=True,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/METADATA",
                    stream=mock.ANY,
                    is_executable=True,
                ),
                mock.call.write_file(
                    scheme="purelib",
                    path="fancy-1.0.0.dist-info/fun_file.txt",
                    stream=mock.ANY,
                    is_executable=False,
                ),
            ],
            any_order=True,
        )

    def test_skips_pycache_and_warns(self, mock_destination):
        """Files inside a __pycache__ directory should be skipped and a
        RuntimeWarning emitted describing the skipped file.
        """
        top_pycache_path = "__pycache__/bad.cpython-312.pyc"
        top_good_path = "good.py"
        sub_pycache_path = "fancy/__pycache__/bad.cpython-312.pyc"
        sub_good_path = "fancy/good.py"

        # Create a fake wheel
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={
                top_pycache_path: b"""\
                        # compiled bytecode
                    """,
                top_good_path: b"""\
                        # good source file
                    """,
                sub_pycache_path: b"""\
                    # compiled bytecode
                """,
                sub_good_path: b"""\
                    # good source file
                """,
            },
            dist_info_files={
                "top_level.txt": b"""\
                    fancy
                """,
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Generator: magic (1.0.0)
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                """,
            },
        )

        # Installing should warn and skip the __pycache__ files.
        with pytest.warns(RuntimeWarning) as record:
            install(
                source=source,
                destination=mock_destination,
                additional_metadata={},
            )

        # Ensure warnings mentioning the __pycache__ paths were emitted.
        messages = [str(w.message) for w in record]
        assert len(messages) == 2
        assert any(top_pycache_path in m for m in messages)
        assert any(sub_pycache_path in m for m in messages)

        # Ensure write_file was not called for the __pycache__ paths.
        written_files = {
            kwargs.get("path")
            for __, kwargs in mock_destination.write_file.call_args_list
        }
        assert top_good_path in written_files
        assert sub_good_path in written_files
        assert top_pycache_path not in written_files
        assert sub_pycache_path not in written_files

        # Ensure finalize_installation's records do not include __pycache__ paths.
        assert mock_destination.finalize_installation.called
        records = mock_destination.finalize_installation.call_args[1]["records"]
        record_paths = {
            rec.path if isinstance(rec, RecordEntry) else rec[0] for __, rec in records
        }
        assert top_good_path in record_paths
        assert sub_good_path in record_paths
        assert top_pycache_path not in record_paths
        assert sub_pycache_path not in record_paths


# --------------------------------------------------------------------------------------
# Component-based selective installation
# --------------------------------------------------------------------------------------
DIST_INFO = "fancy-1.0.0.dist-info"


class TestComponentInstallation:
    def test_overlapping_components_install_each_file_once(self):
        source = make_component_wheel(with_entry_points=False)

        core_only = make_mock_destination()
        install(
            source,
            core_only,
            {},
            components=Components(
                rules={
                    "core": ["fancy/a.py", "fancy/__init__.py"],
                    "all": ["fancy/**"],
                },
                selected=["core"],
                default="exclude",
            ),
        )
        assert set(written_paths(core_only)) == {
            "fancy/a.py",
            "fancy/__init__.py",
            f"{DIST_INFO}/METADATA",
            f"{DIST_INFO}/WHEEL",
        }
        records = final_records(core_only)
        record_ids = [
            (scheme, rec.path if isinstance(rec, RecordEntry) else rec[0])
            for scheme, rec in records
        ]
        # Every source file produces a single record.
        assert len(record_ids) == len(set(record_ids))

        all_component = make_mock_destination()
        install(
            source,
            all_component,
            {},
            components=Components(
                rules={
                    "core": ["fancy/a.py", "fancy/__init__.py"],
                    "all": ["fancy/**"],
                },
                selected=["all"],
                default="exclude",
            ),
        )
        all_paths = set(written_paths(all_component))
        assert "fancy/b.py" in all_paths
        assert "fancy/cli.py" in all_paths
        assert "fancy/data/notes.txt" in all_paths

    def test_data_files_matched_before_relocation(self):
        source = make_component_wheel(with_entry_points=False)

        data_only = make_mock_destination()
        install(
            source,
            data_only,
            {},
            components=Components(
                rules={"data": ["fancy-1.0.0.data/data/**"]},
                selected=["data"],
                default="exclude",
            ),
        )
        data_calls = [
            call.kwargs
            for call in data_only.write_file.call_args_list
            if call.kwargs["path"] != f"{DIST_INFO}/METADATA"
            and call.kwargs["path"] != f"{DIST_INFO}/WHEEL"
        ]
        assert len(data_calls) == 1
        assert data_calls[0]["scheme"] == "data"
        assert data_calls[0]["path"] == "fancy/data.bin"

        whole_data_dir = make_mock_destination()
        install(
            source,
            whole_data_dir,
            {},
            components=Components(
                rules={"data": ["fancy-1.0.0.data/**"]},
                selected=["data"],
                default="exclude",
            ),
        )
        relocated = {
            (call.kwargs["scheme"], call.kwargs["path"])
            for call in whole_data_dir.write_file.call_args_list
        }
        assert ("data", "fancy/data.bin") in relocated
        assert ("scripts", "fancy-tool") in relocated

    def test_entry_point_module_in_other_component_fails_atomically(
        self, mock_destination
    ):
        source = make_component_wheel()

        with pytest.raises(InvalidComponents) as exc_info:
            install(
                source,
                mock_destination,
                {},
                components=Components(
                    rules={
                        "scripts": ["extras/**"],
                        "lib": ["fancy/**"],
                    },
                    selected=["scripts"],
                    default="exclude",
                    entry_points={"fancy": "scripts"},
                ),
            )

        assert "fancy" in str(exc_info.value)
        assert "fancy.cli" in str(exc_info.value)
        # Nothing was written: the failure happened before any output.
        assert mock_destination.mock_calls == []

    def test_script_and_module_in_different_components_both_selected(
        self, mock_destination
    ):
        source = make_component_wheel()
        captured = {}

        def capture(scheme, path, stream, is_executable):
            captured[(scheme, path)] = stream.read()
            return (path, scheme, 0)

        mock_destination.write_file.side_effect = capture

        install(
            source,
            mock_destination,
            {},
            components=Components(
                rules={
                    "scripts": ["extras/**"],
                    "lib": ["fancy/**"],
                },
                selected=["scripts", "lib"],
                default="exclude",
                entry_points={"fancy": "scripts"},
            ),
        )

        # Only the explicitly-selected entry point gets a script.
        assert written_script_names(mock_destination) == ["fancy"]

        installed_ep = captured[("purelib", f"{DIST_INFO}/entry_points.txt")]
        assert b"fancy = fancy.cli:main\n" in installed_ep
        assert b"a = fancy.a:main" not in installed_ep
        assert b"b = fancy.b:main" not in installed_ep
        assert b"[gui_scripts]" not in installed_ep
        # Unknown sections survive untouched.
        assert b"[foo.plugins]\nflurb = some.module:thing\n" in installed_ep

    def test_exempt_module_allows_extension_backed_entry(self, mock_destination):
        source = make_component_wheel()

        install(
            source,
            mock_destination,
            {},
            components=Components(
                rules={"scripts": ["extras/**"]},
                selected=["scripts"],
                default="exclude",
                entry_points={"fancy": "scripts"},
                exempt_modules=["fancy.cli"],
            ),
        )

        assert written_script_names(mock_destination) == ["fancy"]
        paths = set(written_paths(mock_destination))
        assert "fancy/cli.py" not in paths
        assert f"{DIST_INFO}/entry_points.txt" in paths

    def test_unicode_paths_and_entry_point(self, mock_destination):
        source = make_component_wheel(unicode_entry=True)

        install(
            source,
            mock_destination,
            {},
            components=Components(
                rules={"unicode": ["fancy/*.txt", "ünïcode/**"]},
                selected=["unicode"],
                default="exclude",
                entry_points={"u": "unicode"},
            ),
        )

        paths = set(written_paths(mock_destination))
        assert "fancy/数据.txt" in paths
        assert "ünïcode/__init__.py" in paths
        assert "fancy/a.py" not in paths
        assert written_script_names(mock_destination) == ["u"]

    def test_select_all_equivalent_to_full_install(self):
        source = make_component_wheel()

        full = make_mock_destination()
        install(source, full, {})

        select_all = make_mock_destination()
        install(
            source,
            select_all,
            {},
            components=Components(
                rules={"all": ["**"]},
                selected=None,
                default="keep",
            ),
        )

        assert written_script_names(full) == written_script_names(select_all)
        assert sorted(
            (call.kwargs["scheme"], call.kwargs["path"])
            for call in full.write_file.call_args_list
        ) == sorted(
            (call.kwargs["scheme"], call.kwargs["path"])
            for call in select_all.write_file.call_args_list
        )

        def normalize_records(mock_dest):
            return sorted(
                (
                    scheme,
                    record.path if isinstance(record, RecordEntry) else record[0],
                )
                for scheme, record in final_records(mock_dest)
            )

        assert normalize_records(full) == normalize_records(select_all)

    def test_mandatory_files_survive_default_exclude(self, mock_destination):
        source = make_component_wheel()
        captured = {}

        def capture(scheme, path, stream, is_executable):
            captured[(scheme, path)] = stream.read()
            return (path, scheme, 0)

        mock_destination.write_file.side_effect = capture

        install(
            source,
            mock_destination,
            {},
            components=Components(default="exclude"),
        )

        assert written_script_names(mock_destination) == []
        assert set(written_paths(mock_destination)) == {
            f"{DIST_INFO}/METADATA",
            f"{DIST_INFO}/WHEEL",
            f"{DIST_INFO}/entry_points.txt",
        }
        installed_ep = captured[("purelib", f"{DIST_INFO}/entry_points.txt")]
        assert b"[console_scripts]" not in installed_ep
        assert b"[gui_scripts]" not in installed_ep
        assert b"[foo.plugins]\nflurb = some.module:thing\n" in installed_ep

        record_paths = {
            rec.path if isinstance(rec, RecordEntry) else rec[0]
            for _scheme, rec in final_records(mock_destination)
        }
        assert f"{DIST_INFO}/RECORD" in record_paths

    def test_entry_points_file_omitted_when_nothing_remains(self, mock_destination):
        source = FakeWheelSource(
            distribution="fancy",
            version="1.0.0",
            regular_files={"fancy/__init__.py": b"# fancy\n"},
            dist_info_files={
                "WHEEL": b"""\
                    Wheel-Version: 1.0
                    Root-Is-Purelib: true
                    Tag: py3-none-any
                """,
                "METADATA": b"""\
                    Metadata-Version: 2.1
                    Name: fancy
                    Version: 1.0.0
                """,
                "entry_points.txt": b"""\
                    [console_scripts]
                    fancy = fancy:main
                """,
            },
        )

        install(
            source,
            mock_destination,
            {},
            components=Components(default="exclude"),
        )

        assert written_script_names(mock_destination) == []
        paths = set(written_paths(mock_destination))
        assert f"{DIST_INFO}/entry_points.txt" not in paths
        record_paths = {
            rec.path if isinstance(rec, RecordEntry) else rec[0]
            for _scheme, rec in final_records(mock_destination)
        }
        assert f"{DIST_INFO}/entry_points.txt" not in record_paths

    def test_entry_point_module_under_data_platlib(self, mock_destination):
        source = make_component_wheel(platlib_module=True)

        install(
            source,
            mock_destination,
            {},
            components=Components(
                rules={"ext": ["fancy-1.0.0.data/platlib/**"]},
                selected=["ext"],
                default="exclude",
                entry_points={"x": "ext"},
            ),
        )

        assert written_script_names(mock_destination) == ["x"]
        assert ("platlib", "pkg/x.py") in {
            (call.kwargs["scheme"], call.kwargs["path"])
            for call in mock_destination.write_file.call_args_list
        }

    def test_end_to_end_records_bytecode_and_no_deletion(self, tmp_path):
        source = make_component_wheel()

        scheme_dict = {}
        for scheme in SCHEME_NAMES:
            path = tmp_path / scheme
            path.mkdir()
            scheme_dict[scheme] = str(path)

        purelib = Path(scheme_dict["purelib"])
        scripts = Path(scheme_dict["scripts"])

        # A file already present in the environment must never be removed as
        # part of excluding components.
        preexisting = purelib / "fancy" / "preexisting.sentinel"
        preexisting.parent.mkdir(parents=True)
        preexisting.write_text("untouched")

        destination = SchemeDictionaryDestination(
            scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
            bytecode_optimization_levels=[0],
        )

        install(
            source,
            destination,
            {"INSTALLER": b"test-installer"},
            components=Components(
                rules={"core": ["fancy/__init__.py", "fancy/a.py"]},
                selected=["core"],
                default="exclude",
                entry_points={"a": "core"},
            ),
        )

        # Only the selected entry point generated a script.
        assert (scripts / "a").is_file()
        assert not (scripts / "b").exists()
        assert not (scripts / "g").exists()
        assert "from fancy.a import main" in (scripts / "a").read_text()

        # Only selected modules remain.
        assert (purelib / "fancy" / "__init__.py").is_file()
        assert (purelib / "fancy" / "a.py").is_file()
        assert not (purelib / "fancy" / "b.py").exists()
        assert preexisting.is_file()
        assert preexisting.read_text() == "untouched"

        # METADATA is passed through verbatim (dependencies not rewritten).
        metadata_path = purelib / DIST_INFO / "METADATA"
        assert metadata_path.read_bytes() == source.read_dist_info("METADATA").encode(
            "utf-8"
        )

        # The installed entry_points.txt lists only the selected entry, while
        # unknown sections are preserved.
        installed_ep = (purelib / DIST_INFO / "entry_points.txt").read_bytes()
        assert b"a = fancy.a:main" in installed_ep
        assert b"b = fancy.b:main" not in installed_ep
        assert b"g = fancy.gui:main" not in installed_ep
        assert b"[foo.plugins]" in installed_ep

        # RECORD has recomputed hashes/sizes for the rewritten EP, generated
        # script and every kept file, and no rows for excluded content.
        record_path = purelib / DIST_INFO / "RECORD"
        rows = parse_record_file(record_path.read_text().splitlines())
        records = {row[0]: RecordEntry.from_elements(*row) for row in rows}

        expected_purelib_rows = {
            "fancy/__init__.py",
            "fancy/a.py",
            f"{DIST_INFO}/METADATA",
            f"{DIST_INFO}/WHEEL",
            f"{DIST_INFO}/entry_points.txt",
            f"{DIST_INFO}/INSTALLER",
            f"{DIST_INFO}/RECORD",
        }
        script_rows = {key for key in records if "scripts/" in key.replace("\\", "/")}
        assert set(records) - script_rows == expected_purelib_rows
        assert len(script_rows) == 1
        assert next(iter(script_rows)).replace("\\", "/").endswith("scripts/a")

        ep_record = records[f"{DIST_INFO}/entry_points.txt"]
        assert ep_record.validate(installed_ep)
        a_record = records["fancy/a.py"]
        assert a_record.validate((purelib / "fancy" / "a.py").read_bytes())

        # Bytecode is only generated for kept .py files.
        pyc_stems = {path.name.split(".")[0] for path in tmp_path.rglob("*.pyc")}
        assert pyc_stems == {"__init__", "a"}
