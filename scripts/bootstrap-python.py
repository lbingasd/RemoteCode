#!/usr/bin/env python3
"""Bootstrap a private Linux Python 3.12 using the distribution's Python 3.8+.

Keep this file and its imported download helpers parseable by Python 3.8.
Never replace /usr/bin/python3 or add a third-party apt Python repository.
"""
import argparse
from pathlib import Path, PurePosixPath
import platform
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile

from common import download, read, require, sha256, write
from transfer import segmented_download

PACKAGE = Path(__file__).resolve().parent.parent


def extract_runtime(archive, destination):
    """Conservative extraction usable before tarfile's data filter was introduced.

    Reject special files, hard links and any archive write through a symlink.
    Relative symlinks are created only after all regular files have been written.
    No archive ownership, setuid/setgid bits or writable group modes are restored.
    """
    destination = Path(destination)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        validate_members(members)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for member in members:
            if member.issym():
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)


def validate_members(members):
    links = set()
    names = set()
    for member in members:
        path = PurePosixPath(member.name)
        require(not path.is_absolute() and path.parts and path.parts[0] == "python"
                and ".." not in path.parts and "\\" not in member.name, "Python 归档包含越界路径")
        require(str(path) not in names, "Python 归档包含重复路径")
        names.add(str(path))
        require(member.isdir() or member.isfile() or member.issym(), "Python 归档包含不支持的特殊文件/硬链接")
        if member.issym():
            target = PurePosixPath(member.linkname)
            resolved = PurePosixPath(posixpath.normpath(str(path.parent / target)))
            require(member.linkname and not target.is_absolute() and resolved.parts and resolved.parts[0] == "python"
                    and ".." not in resolved.parts and "\\" not in member.linkname, "Python 归档包含不安全的符号链接")
            links.add(str(path))
    for member in members:
        path = PurePosixPath(member.name)
        require(not any(str(parent) in links for parent in path.parents), "拒绝通过归档符号链接写入文件")
        if member.issym():
            target = PurePosixPath(posixpath.normpath(str(path.parent / member.linkname)))
            require(not any(str(parent) in links for parent in target.parents), "拒绝引用经过符号链接目录的目标")


def install(prefix, cache, item, mirror=None):
    import fcntl
    prefix = prefix.resolve()
    require(prefix not in (Path.home(), PACKAGE, Path.cwd(), Path(prefix.anchor)), "私有 Python 必须使用独立目录")
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (prefix.parent / ("." + prefix.name + ".bootstrap.lock")).open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("另一进程正在引导私有 Python，等待它结束后重跑")
        return _install(prefix, cache, item, mirror)


def _install(prefix, cache, item, mirror=None):
    """Stage and probe before publishing; a failed bootstrap leaves only cache."""
    prefix = prefix.resolve()
    binary = prefix / "bin/python3"
    identity = {"archive_sha256": item["sha256"], "name": item["name"]}
    if (prefix / "remotecode-python.json").is_file():
        require(read(prefix / "remotecode-python.json") == identity, "现有私有 Python 版本不同，不会自动替换")
        probe(binary)
        return binary
    require(not prefix.exists() or not any(prefix.iterdir()), "私有 Python 目录非空且没有有效安装记录，请检查后移走该目录再重跑")
    require(Path(item["name"]).name == item["name"], "Python 缓存文件名不合法")
    archive = cache / item["name"]
    try:
        segmented_download(item["url"], archive, item["sha256"], mirror, workers=8)
    except RuntimeError:
        print("Python 分段下载暂不可用，尝试普通校验续传。", flush=True)
        download(item["url"], archive, item["sha256"], mirror)
    require(sha256(archive) == item["sha256"].lower(), "Python 安装包校验失败")
    with tempfile.TemporaryDirectory(prefix=".python-install-", dir=str(prefix.parent)) as folder:
        stage = Path(folder)
        extract_runtime(archive, stage)
        runtime = stage / "python"
        probe(runtime / "bin/python3")
        write(runtime / "remotecode-python.json", identity)
        if prefix.exists():
            require(not any(prefix.iterdir()), "安装期间目标被写入，拒绝覆盖")
            prefix.rmdir()
        runtime.replace(prefix)
    print("私有 Python 安装完成: " + str(binary), flush=True)
    return binary


def probe(binary):
    subprocess.run([str(binary), "-I", "-c",
                    "import sys,ssl,tarfile,tomllib; assert sys.version_info >= (3,12); print(sys.version.split()[0])"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--mirror")
    args = parser.parse_args()
    require(sys.version_info >= (3, 8), "引导程序需要系统 Python 3.8+")
    require(platform.system() == "Linux", "此私有 Python 引导只用于 Linux")
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    require(arch, "Python 引导仅支持 Linux x64/ARM64")
    lock = read(PACKAGE / "examples/python-bootstrap.lock.json")
    install(args.prefix, args.cache.expanduser().resolve(), lock["files"]["linux-" + arch], args.mirror)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("私有 Python 安装未完成: " + str(exc) + "；缓存保留，可重跑或提供 --mirror。", file=sys.stderr)
        sys.exit(1)
