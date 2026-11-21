import base64
import os
import shutil
import zipfile
from pathlib import Path

from sap_pipe.log import get_logger

log = get_logger()


def exists(path: str) -> bool:
    return os.path.exists(path)


def copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)


def _copy_dir(src: str, dst: str) -> None:
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isdir(src):
        raise OSError(f"source {src} is not a directory")
    if os.path.exists(dst):
        raise OSError(f"destination {dst} already exists")
    os.makedirs(dst, exist_ok=True)
    for entry in os.scandir(src):
        src_path = os.path.join(src, entry.name)
        dst_path = os.path.join(dst, entry.name)
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            _copy_dir(src_path, dst_path)
        elif entry.is_file(follow_symlinks=False):
            copy_file(src_path, dst_path)


def replace_dir(src: str, dst: str) -> None:
    shutil.rmtree(dst, ignore_errors=True)
    _copy_dir(src, dst)


def unzip_source(source: str, destination: str) -> None:
    destination = os.path.abspath(destination)
    os.makedirs(destination, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            target = os.path.abspath(os.path.join(destination, info.filename))
            if not target.startswith(destination + os.sep) and target != destination:
                raise OSError(f"invalid file path: {target}")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def zip_dir(src: str, dst: str, include_src: bool = False) -> None:
    src = os.path.abspath(src)
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    base = os.path.dirname(src) if include_src else src
    with zipfile.ZipFile(dst, "w") as archive:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames.sort()
            filenames.sort()
            if not (dirpath == src and not include_src):
                rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
                if rel_dir != ".":
                    archive.writestr(rel_dir.rstrip("/") + "/", b"")
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                arcname = os.path.relpath(full, base).replace(os.sep, "/")
                archive.write(full, arcname, compress_type=zipfile.ZIP_DEFLATED)


def zip_dir_to_base64(src: str) -> str:
    zip_file = src + ".zip"
    try:
        zip_dir(src, zip_file, include_src=False)
        content = Path(zip_file).read_bytes()
    finally:
        if os.path.exists(zip_file):
            os.remove(zip_file)
    return base64.b64encode(content).decode("ascii")


_SKIP_NAMES = {"parameters.prop", ".DS_Store"}


def _normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _text(path: str) -> str:
    return _normalized(Path(path).read_bytes()).decode("utf-8", errors="surrogateescape")


def _manifest_body(path: str) -> list[str]:
    return [line for line in _text(path).splitlines() if not line.startswith("Origin")]


def _properties_body(path: str) -> list[str]:
    kept = []
    for line in _text(path).splitlines():
        stripped = line.lstrip()
        if not stripped or stripped[0] in "#!":
            continue
        kept.append(line)
    return kept


def _relative_files(root: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name in _SKIP_NAMES:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found[rel] = full
    return found


def diff_directories(first_dir: str, second_dir: str) -> bool:
    """Return True when the two trees differ.

    Line endings are ignored. Origin-* headers are ignored only inside
    MANIFEST.MF. Spaces inside scripts, maps and other files are significant.
    """
    if not os.path.isdir(first_dir) or not os.path.isdir(second_dir):
        log.info("Directory missing while comparing %s and %s", first_dir, second_dir)
        return True
    left = _relative_files(first_dir)
    right = _relative_files(second_dir)
    if left.keys() != right.keys():
        log.info("File set differs between %s and %s", first_dir, second_dir)
        return True
    for rel in sorted(left):
        if diff_file(left[rel], right[rel]):
            log.info("Content differs: %s", rel)
            return True
    return False


def diff_file(first_file: str, second_file: str, ignore_hash_comments: bool = False) -> bool:
    """Return True when the two files differ. Spaces inside the file count."""
    if not os.path.isfile(first_file) or not os.path.isfile(second_file):
        log.info("File missing while comparing %s and %s", first_file, second_file)
        return True
    name = os.path.basename(first_file)
    if name == "MANIFEST.MF":
        return _manifest_body(first_file) != _manifest_body(second_file)
    if ignore_hash_comments:
        return _properties_body(first_file) != _properties_body(second_file)
    return _normalized(Path(first_file).read_bytes()) != _normalized(Path(second_file).read_bytes())


def diff_optional_file(src_dir: str, tgt_dir: str, relative_path: str) -> bool:
    downloaded = os.path.join(src_dir, relative_path)
    git_file = os.path.join(tgt_dir, relative_path)
    if exists(downloaded) and exists(git_file):
        comments = relative_path.endswith("parameters.prop")
        return diff_file(downloaded, git_file, ignore_hash_comments=comments)
    if not exists(downloaded) and not exists(git_file):
        log.warning("Skipping diff of %s as it does not exist in both source and target", relative_path)
        return False
    log.info("File %s does not exist in either source or target", relative_path)
    return True
