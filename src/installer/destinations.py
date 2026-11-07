"""Handles all file writing and post-installation processing."""

import io
import os
import posixpath
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    Union,
)

from installer.exceptions import (
    BytecodeCompilationError,
    InvalidSourceDateEpoch,
)
from installer.records import Hash, RecordEntry
from installer.scripts import Script
from installer.utils import (
    DEFAULT_SOURCE_DATE_EPOCH,
    _WINDOWS,
    Scheme,
    construct_record_file,
    copyfileobj_with_hashing,
    fix_shebang,
    make_file_executable,
    normalize_bytecode_optimization_levels,
    source_date_epoch_to_zip_date_time,
)

if TYPE_CHECKING:
    from installer.scripts import LauncherKind, ScriptSection


class WheelDestination:
    """Handles writing the unpacked files, script generation and ``RECORD`` generation.

    Subclasses provide the concrete script generation logic, as well as the RECORD file
    (re)writing.
    """

    def write_script(
        self, name: str, module: str, attr: str, section: "ScriptSection"
    ) -> RecordEntry:
        """Write a script in the correct location to invoke given entry point.

        :param name: name of the script
        :param module: module path, to load the entry point from
        :param attr: final attribute access, for the entry point
        :param section: Denotes the "entry point section" where this was specified.
            Valid values are ``"gui"`` and ``"console"``.
        :type section: str

        Example usage/behaviour::

            >>> dest.write_script("pip", "pip._internal.cli", "main", "console")

        """
        raise NotImplementedError

    def write_file(
        self,
        scheme: Scheme,
        path: Union[str, "os.PathLike[str]"],
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write a file to correct ``path`` within the ``scheme``.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        The stream would be closed by the caller, after this call.

        Example usage/behaviour::

            >>> with open("__init__.py") as stream:
            ...     dest.write_file("purelib", "pkg/__init__.py", stream)

        """
        raise NotImplementedError

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        """Finalize installation, after all the files are written.

        Handles (re)writing of the ``RECORD`` file.

        :param scheme: scheme to write the ``RECORD`` file in
        :param record_file_path: path of the ``RECORD`` file with that scheme
        :param records: entries to write to the ``RECORD`` file

        Example usage/behaviour::

            >>> dest.finalize_installation("purelib")

        """
        raise NotImplementedError


@dataclass
class SchemeDictionaryDestination(WheelDestination):
    """Destination, based on a mapping of {scheme: file-system-path}."""

    scheme_dict: dict[str, str]
    """A mapping of {scheme: file-system-path}"""

    interpreter: str
    """The interpreter to use for generating scripts."""

    script_kind: "LauncherKind"
    """The "kind" of launcher script to use."""

    hash_algorithm: str = "sha256"
    """
    The hashing algorithm to use, which is a member of
    :any:`hashlib.algorithms_available` (ideally from
    :any:`hashlib.algorithms_guaranteed`).
    """

    bytecode_optimization_levels: Collection[int] = ()
    """
    Compile cached bytecode for installed .py files with these optimization
    levels. The bytecode is specific to the minor version of Python (e.g. 3.10)
    used to generate it.
    """

    destdir: str | None = None
    """
    A staging directory in which to write all files. This is expected to be the
    filesystem root at runtime, so embedded paths will be written as though
    this was the root.
    """

    overwrite_existing: bool = False
    """Silently overwrite existing files."""

    reproducible: bool = False
    """
    Produce reproducible installation artifacts.

    When enabled, installing the same wheel into different staging
    directories (``destdir``), for the same scheme paths, interpreter and
    optimization levels, results in byte-for-byte identical installed files,
    generated entry scripts and ``RECORD`` (filesystem timestamps of the
    written files are not part of the artifacts).

    Concretely this means:

    - bytecode is compiled using checked-hash invalidation (see
      :pep:`552`), so ``.pyc`` files embed a hash of the source instead of
      its modification time or size; the embedded ``co_filename`` is the
      final runtime path (derived from ``scheme_dict``), never ``destdir``
      or a temporary directory;
    - ``bytecode_optimization_levels`` is de-duplicated and only the levels
      0, 1 and 2 are accepted;
    - compilation errors fail the installation and refer to the logical
      (final runtime) source path;
    - Windows entry launchers embed ZIP archives with fixed member
      date/time, order, permissions and compression -- never the current
      wall-clock time;
    - compiled ``.pyc`` files are recorded in ``RECORD`` with the hash and
      size of the bytes actually written.

    When disabled (the default), behaviour is unchanged.
    """

    source_date_epoch: int | None = None
    """
    POSIX timestamp (seconds since the UNIX epoch, interpreted in UTC) used
    for the fixed metadata of reproducible artifacts. Only used when
    ``reproducible`` is enabled.

    When ``None``, :data:`installer.utils.DEFAULT_SOURCE_DATE_EPOCH`
    (1980-01-01 00:00:00 UTC) is used. Values outside of the range that ZIP
    archive members can represent (1980-01-01 00:00:00 UTC to
    2107-12-31 23:59:58 UTC) are rejected.
    """

    def __post_init__(self) -> None:
        if not self.reproducible:
            if self.source_date_epoch is not None:
                message = (
                    "source_date_epoch can only be used together with"
                    " reproducible=True."
                )
                raise InvalidSourceDateEpoch(message)
            return

        self.bytecode_optimization_levels = normalize_bytecode_optimization_levels(
            self.bytecode_optimization_levels
        )
        if self.source_date_epoch is None:
            self.source_date_epoch = DEFAULT_SOURCE_DATE_EPOCH
        else:
            # Validate eagerly, so invalid timestamps fail before any file is
            # written, with a clear error message.
            source_date_epoch_to_zip_date_time(self.source_date_epoch)

    def _path_with_destdir(self, scheme: Scheme, path: str) -> Path:
        # See https://docs.python.org/3/library/zipfile.html#zipfile.Path:
        #  When handling untrusted archives,
        #  consider resolving filenames using os.path.abspath()
        #  and checking against the target directory with os.path.commonpath().
        #
        # Attention: Path.absolute() is not sufficient because it does not
        #  normalize, i.e. does not remove "..".
        #
        # We want to avoid Path.resolve() because it is significantly slower
        # than os.path.abspath()!
        target_dir = Path(os.path.abspath(self.scheme_dict[scheme]))  # noqa: PTH100
        file = Path(os.path.abspath(target_dir / path))  # noqa: PTH100

        if not file.is_relative_to(target_dir):
            raise ValueError(
                f"Attempting to write {path} outside of the target directory"
            )

        if self.destdir is not None:
            rel_path = file.relative_to(file.anchor)
            return Path(self.destdir) / rel_path
        return file

    def write_to_fs(
        self,
        scheme: Scheme,
        path: str,
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write contents of ``stream`` to the correct location on the filesystem.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        - Ensures that an existing file is not being overwritten.
        - Hashes the written content, to determine the entry in the ``RECORD`` file.
        """
        target_path = self._path_with_destdir(scheme, path)
        if not self.overwrite_existing and target_path.exists():
            message = f"File already exists: {target_path!s}"
            raise FileExistsError(message)

        parent_folder = target_path.parent
        if not parent_folder.exists():
            parent_folder.mkdir(parents=True)

        with target_path.open("wb") as f:
            hash_, size = copyfileobj_with_hashing(stream, f, self.hash_algorithm)

        if is_executable:
            make_file_executable(target_path)

        return RecordEntry(path, Hash(self.hash_algorithm, hash_), size)

    def write_file(
        self,
        scheme: Scheme,
        path: Union[str, "os.PathLike[str]"],
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write a file to correct ``path`` within the ``scheme``.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        - Changes the shebang for files in the "scripts" scheme.
        - Uses :py:meth:`SchemeDictionaryDestination.write_to_fs` for the
          filesystem interaction.
        """
        path_ = os.fspath(path)

        if scheme == "scripts":
            with fix_shebang(stream, self.interpreter) as stream_with_different_shebang:
                return self.write_to_fs(
                    scheme, path_, stream_with_different_shebang, is_executable
                )

        return self.write_to_fs(scheme, path_, stream, is_executable)

    def write_script(
        self, name: str, module: str, attr: str, section: "ScriptSection"
    ) -> RecordEntry:
        """Write a script to invoke an entrypoint.

        :param name: name of the script
        :param module: module path, to load the entry point from
        :param attr: final attribute access, for the entry point
        :param section: Denotes the "entry point section" where this was specified.
            Valid values are ``"gui"`` and ``"console"``.
        :type section: str

        - Generates a launcher using :any:`Script.generate`.
        - Writes to the "scripts" scheme.
        - Uses :py:meth:`SchemeDictionaryDestination.write_to_fs` for the
          filesystem interaction.
        """
        script = Script(name, module, attr, section)
        if self.reproducible:
            script_name, data = script.generate(
                self.interpreter,
                self.script_kind,
                source_date_epoch=self.source_date_epoch,
            )
        else:
            script_name, data = script.generate(self.interpreter, self.script_kind)

        with io.BytesIO(data) as stream:
            entry = self.write_to_fs(
                Scheme("scripts"), script_name, stream, is_executable=True
            )

            path = self._path_with_destdir(Scheme("scripts"), script_name)
            mode = path.stat().st_mode
            mode |= (mode & 0o444) >> 2
            path.chmod(mode)

            return entry

    def _compile_bytecode(
        self, scheme: Scheme, record: RecordEntry
    ) -> list[RecordEntry]:
        """Compile bytecode for a single ``.py`` file.

        In the default mode this uses :mod:`compileall`'s timestamp-based
        invalidation and returns an empty list (the generated ``.pyc`` files
        are not part of ``RECORD``).

        In reproducible mode the bytecode uses checked-hash invalidation and
        one :class:`~installer.records.RecordEntry` per generated ``.pyc``
        file is returned, so the caller can record the actual hash and size
        of every final artifact.
        """
        if not self.bytecode_optimization_levels or scheme not in (
            "purelib",
            "platlib",
        ):
            return []
        if not record.path.endswith(".py"):
            return []

        target_path = self._path_with_destdir(scheme, record.path)
        logical_path = Path(self.scheme_dict[scheme]) / record.path

        if not self.reproducible:
            import compileall

            dir_path_to_embed = logical_path.parent

            for level in self.bytecode_optimization_levels:
                compileall.compile_file(
                    target_path, optimize=level, quiet=1, ddir=dir_path_to_embed
                )
            return []

        import py_compile

        pyc_records: list[RecordEntry] = []
        for level in self.bytecode_optimization_levels:
            try:
                # ``dfile`` is the path embedded into the bytecode (eg. in
                # ``co_filename`` and tracebacks); point it at the final
                # runtime location rather than the staging directory. The
                # checked-hash invalidation mode embeds a hash of the source
                # contents instead of their modification time/size.
                pyc_filename = py_compile.compile(
                    str(target_path),
                    dfile=str(logical_path),
                    doraise=True,
                    quiet=1,
                    optimize=level,
                    invalidation_mode=(
                        py_compile.PycInvalidationMode.CHECKED_HASH
                    ),
                )
            except py_compile.PyCompileError as exc:
                message = (
                    "Failed to byte-compile"
                    f" {logical_path} at optimization level {level}: {exc.msg}"
                )
                raise BytecodeCompilationError(message) from exc

            assert pyc_filename is not None  # guaranteed with doraise=True
            pyc_path = Path(pyc_filename)
            pyc_record_path = posixpath.join(
                posixpath.dirname(record.path), "__pycache__", pyc_path.name
            )
            sink = io.BytesIO()
            with pyc_path.open("rb") as pyc_stream:
                hash_, size = copyfileobj_with_hashing(
                    pyc_stream, sink, self.hash_algorithm
                )
            pyc_records.append(
                RecordEntry(
                    pyc_record_path, Hash(self.hash_algorithm, hash_), size
                )
            )
        return pyc_records

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        """Finalize installation, by writing the ``RECORD`` file & compiling bytecode.

        :param scheme: scheme to write the ``RECORD`` file in
        :param record_file_path: path of the ``RECORD`` file with that scheme
        :param records: entries to write to the ``RECORD`` file
        """

        def prefix_for_scheme(file_scheme: str) -> str | None:
            if file_scheme == scheme:
                return None
            if _WINDOWS:  # pragma: no cover
                path = os.path.abspath(self.scheme_dict[file_scheme])  # noqa: PTH100
            else:  # pragma: no cover
                path = os.path.relpath(
                    self.scheme_dict[file_scheme],
                    start=self.scheme_dict[scheme],
                )
            return path + "/"

        record_list = list(records)

        # In reproducible mode, compile bytecode before writing ``RECORD`` so
        # that every final artifact (including the generated ``.pyc`` files)
        # is recorded with the hash and size of the bytes actually written.
        if self.reproducible:
            pyc_records: list[tuple[Scheme, RecordEntry]] = []
            for scheme_, record in record_list:
                pyc_records.extend(
                    (scheme_, pyc_record)
                    for pyc_record in self._compile_bytecode(scheme_, record)
                )
            record_list.extend(pyc_records)

        with construct_record_file(record_list, prefix_for_scheme) as record_stream:
            self.write_to_fs(
                scheme, record_file_path, record_stream, is_executable=False
            )

        if not self.reproducible:
            for scheme_, record in record_list:
                self._compile_bytecode(scheme_, record)
