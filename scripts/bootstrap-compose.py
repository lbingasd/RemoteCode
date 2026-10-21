#!/usr/bin/env python3
"""Install a verified system-wide Compose plugin without changing Docker packages."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from common import download, progress, read, require, sha256
from transfer import segmented_download

PACKAGE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = Path("/usr/local/lib/docker/cli-plugins")


def probe(binary, version):
    result = subprocess.run([str(binary), "docker-cli-plugin-metadata"], check=True,
                            capture_output=True, text=True, timeout=30)
    metadata = json.loads(result.stdout)
    require(metadata.get("Version") == "v" + version and metadata.get("SchemaVersion") == "0.1.0",
            "Compose 插件元数据/版本不符合锁定记录")


def install(directory, cache, item, version, mirror=None):
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    target = directory / "docker-compose"
    # Do not overwrite an unknown/broken plugin or a symlink owned by another installer.
    with (directory / ".remotecode-compose.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            require(not target.is_symlink() and target.is_file() and sha256(target) == item["sha256"],
                    "已有 Compose 插件不可用且不属于当前锁定版本，未覆盖: " + str(target))
            probe(target, version)
            progress("已安装的 Compose 插件校验通过，直接复用")
            return target
        require(Path(item["name"]).name == item["name"], "Compose 缓存文件名不合法")
        binary = Path(cache) / item["name"]
        progress("下载/恢复已锁定的 Docker Compose " + version)
        try:
            segmented_download(item["url"], binary, item["sha256"], mirror)
        except RuntimeError:
            progress("Compose 分段下载暂不可用，尝试普通校验续传")
            download(item["url"], binary, item["sha256"], mirror)
        require(sha256(binary) == item["sha256"], "Compose 插件 SHA-256 校验失败")
        # Copy into a private stage, validate before publication; keep download cache on failure.
        with tempfile.TemporaryDirectory(prefix=".remotecode-compose-", dir=directory) as folder:
            stage = Path(folder) / "docker-compose"
            shutil.copyfile(binary, stage)
            stage.chmod(0o755)
            require(sha256(stage) == item["sha256"], "Compose 插件复制后校验失败")
            probe(stage, version)
            require(not target.exists() and not target.is_symlink(), "安装期间目标被写入，拒绝覆盖")
            stage.replace(target)
        progress("Compose 插件安装完成: " + str(target))
        return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--mirror")
    args = parser.parse_args()
    require(platform.system() == "Linux" and os.geteuid() == 0, "Compose 引导仅用于 Linux，需 root 权限")
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    require(arch, "Compose 引导仅支持 Linux x64/ARM64")
    lock = read(PACKAGE / "examples/compose-plugin.lock.json")
    install(PLUGIN_DIR, args.cache.expanduser().resolve(), lock["files"]["linux-" + arch],
            lock["version"], args.mirror)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # Avoid echoing download URLs containing operator credentials.
        print("Compose 安装未完成 (" + type(exc).__name__ + ")；缓存保留。检查已有插件、权限和镜像后重跑。",
              file=sys.stderr)
        sys.exit(1)
