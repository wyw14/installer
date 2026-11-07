"""Core wheel installation logic."""

import posixpath
import warnings
from io import BytesIO
from typing import TYPE_CHECKING, cast

from installer.components import _Selector, module_file_candidates, rewrite_entry_points
from installer.destinations import WheelDestination
from installer.exceptions import InvalidComponents, InvalidWheelSource
from installer.records import RecordEntry
from installer.sources import WheelSource
from installer.utils import SCHEME_NAMES, Scheme, parse_entrypoints, parse_metadata_file

if TYPE_CHECKING:
    from installer.components import Components
    from installer.scripts import ScriptSection

__all__ = ["install"]


def _process_WHEEL_file(source: WheelSource) -> Scheme:  # noqa: N802
    """Process the WHEEL file, from ``source``.

    Returns the scheme that the archive root should go in.
    """
    stream = source.read_dist_info("WHEEL")
    metadata = parse_metadata_file(stream)

    # Ensure compatibility with this wheel version.
    if not (metadata["Wheel-Version"] and metadata["Wheel-Version"].startswith("1.")):
        message = "Incompatible Wheel-Version {}, only support version 1.x wheels."
        raise InvalidWheelSource(source, message.format(metadata["Wheel-Version"]))

    # Determine where archive root should go.
    if metadata["Root-Is-Purelib"] == "true":
        return cast("Scheme", "purelib")
    else:
        return cast("Scheme", "platlib")


def _determine_scheme(
    path: str, source: WheelSource, root_scheme: Scheme
) -> tuple[Scheme, str]:
    """Determine which scheme to place given path in, from source."""
    data_dir = source.data_dir

    # If it's in not `{distribution}-{version}.data`, then it's in root_scheme.
    if posixpath.commonpath([data_dir, path]) != data_dir:
        return root_scheme, path

    # Figure out which scheme this goes to.
    parts = []
    scheme_name = None
    left = path
    while True:
        left, right = posixpath.split(left)
        parts.append(right)
        if left == source.data_dir:
            scheme_name = right
            break

    if scheme_name not in SCHEME_NAMES:
        msg_fmt = "{path} is not contained in a valid .data subdirectory."
        raise InvalidWheelSource(source, msg_fmt.format(path=path))

    return cast("Scheme", scheme_name), posixpath.join(*reversed(parts[:-1]))


def _is_pycache_path(path: str) -> bool:
    return "__pycache__" in path.split("/")[:-1]


def _plan_installation(
    source: WheelSource,
    root_scheme: Scheme,
    *,
    record_file_path: str,
    components: "Components | None",
) -> tuple[
    "set[str] | None",
    "list[tuple[str, str, str, ScriptSection]]",
    "str | None",
]:
    """Plan which files and entry points to install.

    Nothing is written by this function, so every validation failure aborts
    the whole installation before the destination is touched.

    :return: a 3-tuple of:

        - paths (wheel-relative) to install, or ``None`` to install everything,
        - console/gui entry points to generate scripts for,
        - new ``entry_points.txt`` contents to write, or ``None`` to install
          the wheel's file unchanged (empty string means omit the file).
    """
    # First pass over the source: enumerate every wheel path, without reading
    # the file contents. get_contents() is consumed again for the actual
    # writing; advancing this iterator lets each stream be closed by source.
    source_paths = [
        record_elements[0]
        for record_elements, _stream, _is_executable in source.get_contents()
    ]

    selector: _Selector | None = None
    if components is None:
        kept_paths: set[str] | None = None
    else:
        selector = _Selector(components)
        mandatory_paths = {
            posixpath.join(source.dist_info_dir, filename)
            for filename in ("METADATA", "WHEEL")
        }
        kept_paths = {
            path
            for path in source_paths
            if path != record_file_path
            if not _is_pycache_path(path)
            if path in mandatory_paths or selector.keep_path(path)
        }

    # Determine schemes for everything that will be installed (which also
    # validates the .data directory layout), collecting the purelib/platlib
    # paths that can back an entry point's module.
    importable_paths: set[str] = set()
    for path in source_paths:
        if path == record_file_path or _is_pycache_path(path):
            continue
        if kept_paths is not None and path not in kept_paths:
            continue
        scheme, destination_path = _determine_scheme(
            path=path,
            source=source,
            root_scheme=root_scheme,
        )
        if scheme in ("purelib", "platlib"):
            importable_paths.add(destination_path)

    entry_points: list[tuple[str, str, str, ScriptSection]] = []
    new_entry_points_text: str | None = None
    if "entry_points.txt" in source.dist_info_filenames:
        original_entry_points_text = source.read_dist_info("entry_points.txt")
        parsed_entry_points = list(parse_entrypoints(original_entry_points_text))

        if components is None:
            entry_points = parsed_entry_points
        else:
            assert selector is not None
            kept_entry_points: set[tuple[str, str]] = set()
            for name, module, attr, section in parsed_entry_points:
                if selector.keep_entry_point(name):
                    entry_points.append((name, module, attr, section))
                    kept_entry_points.add((section, name))

            # Every kept entry point must be backed by a .py module (or a
            # package __init__.py) in the kept file set. This is a purely
            # static check; modules are never imported.
            for name, module, _attr, section in entry_points:
                if module in components.exempt_modules:
                    continue
                module_py, package_init = module_file_candidates(module)
                if (
                    module_py not in importable_paths
                    and package_init not in importable_paths
                ):
                    message = (
                        f"Selected {section} entry point {name!r} requires module "
                        f"{module!r}, but neither {module_py!r} nor "
                        f"{package_init!r} is installed by the selected "
                        "components. Select the component containing the "
                        "module, or list the module in Components.exempt_modules "
                        "when it is backed by an extension module that cannot be "
                        "detected statically."
                    )
                    raise InvalidComponents(message)

            new_entry_points_text = rewrite_entry_points(
                original_entry_points_text, kept_entry_points
            )

    return kept_paths, entry_points, new_entry_points_text


def install(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
    *,
    components: "Components | None" = None,
) -> None:
    """Install wheel described by ``source`` into ``destination``.

    :param source: wheel to install.
    :param destination: where to write the wheel.
    :param additional_metadata: additional metadata files to generate, usually
                                generated by the caller.
    :param components: optional component selection configuration, for
                        installing only part of the wheel's regular files and
                        entry points. When ``None`` (the default), every file
                        and entry point in the wheel is installed.

    """
    root_scheme = _process_WHEEL_file(source)

    # RECORD handling
    record_file_path = posixpath.join(source.dist_info_dir, "RECORD")
    entry_points_file_path = posixpath.join(source.dist_info_dir, "entry_points.txt")
    written_records = []

    # Decide what to install before writing anything, so invalid configuration
    # fails the installation atomically.
    kept_paths, entry_points, new_entry_points_text = _plan_installation(
        source,
        root_scheme,
        record_file_path=record_file_path,
        components=components,
    )

    # Write the entry_points based scripts.
    for name, module, attr, section in entry_points:
        record = destination.write_script(
            name=name,
            module=module,
            attr=attr,
            section=section,
        )
        written_records.append((Scheme("scripts"), record))

    # Write all the files from the wheel.
    for record_elements, stream, is_executable in source.get_contents():
        source_record = RecordEntry.from_elements(*record_elements)
        path = source_record.path
        # Skip the RECORD, which is written at the end, based on this info.
        if path == record_file_path:
            continue

        if _is_pycache_path(path):
            warnings.warn(
                (
                    f"Skip installing {path} from {source.distribution}."
                    " Installing files in a __pycache__ directory poses a security risk."
                    " __pycache__ directories should not be included in wheels."
                    f" This is probably an issue in the build process of '{source.distribution}'."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        # entry_points.txt is rewritten (or omitted) when components are used.
        # This is independent of file rules: unknown sections are always
        # preserved, even with a default-exclude policy.
        if path == entry_points_file_path and new_entry_points_text is not None:
            if new_entry_points_text:
                scheme, destination_path = _determine_scheme(
                    path=path,
                    source=source,
                    root_scheme=root_scheme,
                )
                with BytesIO(new_entry_points_text.encode("utf-8")) as ep_stream:
                    record = destination.write_file(
                        scheme=scheme,
                        path=destination_path,
                        stream=ep_stream,
                        is_executable=False,
                    )
                written_records.append((scheme, record))
            continue

        # Apply component selection to regular files.
        if kept_paths is not None and path not in kept_paths:
            continue

        # Figure out where to write this file.
        scheme, destination_path = _determine_scheme(
            path=path,
            source=source,
            root_scheme=root_scheme,
        )
        record = destination.write_file(
            scheme=scheme,
            path=destination_path,
            stream=stream,
            is_executable=is_executable,
        )
        written_records.append((scheme, record))

    # Write all the installation-specific metadata
    for filename, contents in additional_metadata.items():
        path = posixpath.join(source.dist_info_dir, filename)

        with BytesIO(contents) as other_stream:
            record = destination.write_file(
                scheme=root_scheme,
                path=path,
                stream=other_stream,
                is_executable=False,
            )
        written_records.append((root_scheme, record))

    written_records.append((root_scheme, RecordEntry(record_file_path, None, None)))
    destination.finalize_installation(
        scheme=root_scheme,
        record_file_path=record_file_path,
        records=written_records,
    )
