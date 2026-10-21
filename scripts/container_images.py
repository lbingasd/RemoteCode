"""Pull the gateway image through mirrors while preserving its content digest."""
import json
import os
from pathlib import Path
import re
import subprocess

from common import activity, progress, read, require, run

PACKAGE = Path(__file__).resolve().parent.parent
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def repository(image):
    require(isinstance(image, str) and image and not image.startswith("-")
            and not any(c.isspace() for c in image) and "://" not in image, "镜像引用格式不正确")
    repo = image.split("@", 1)[0]
    if ":" in repo.rsplit("/", 1)[-1]:
        repo = repo.rsplit(":", 1)[0]
    require(re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*", repo), "镜像仓库格式不正确")
    parts = repo.split("/")
    if parts[0] in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        parts = parts[1:]
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        return repo
    if len(parts) == 1:
        parts.insert(0, "library")
    return "docker.io/" + "/".join(parts)


def pinned_digest(image):
    if "@" not in image:
        return None
    digest = image.rsplit("@", 1)[1]
    require(DIGEST.fullmatch(digest), "镜像 digest 必须是完整 SHA-256")
    return digest


def candidates(image, source, cn=False):
    repository(image)
    expected = pinned_digest(image)
    origin = source or image
    builtin = repository(origin) == "docker.io/library/nginx" and (
        origin.endswith(":stable-alpine") or "@" in origin)
    config = read(PACKAGE / "examples/download-mirrors.json")
    timeout = config.get("docker_pull_timeout_seconds", 180)
    require(type(timeout) is int and 10 <= timeout <= 3600, "Docker 拉取超时须为 10 至 3600 秒")
    if not cn or not builtin:
        return [image], expected, timeout
    if not expected:
        lock = read(PACKAGE / "examples/nginx-image.lock.json")
        require(lock.get("schema") == 1 and lock.get("image") == "docker.io/library/nginx:stable-alpine"
                and DIGEST.fullmatch(lock.get("digest", "")), "nginx 官方镜像锁格式不正确")
        expected = lock["digest"]
    registries = config.get("nginx_registries", [])
    require(isinstance(registries, list), "nginx 镜像源配置必须是数组")
    result = []
    for repo in [*registries, "docker.io/library/nginx"]:
        require(isinstance(repo, str) and "@" not in repo and ":" not in repo.rsplit("/", 1)[-1],
                "nginx 镜像源应只包含仓库路径，不含 tag/digest")
        repository(repo)
        ref = repo + "@" + expected
        if ref not in result:
            result.append(ref)
    return result, expected, timeout


def inspect_digest(image, expected=None):
    raw = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"], text=True)
    digests = json.loads(raw)
    require(isinstance(digests, list), "镜像没有有效 RepoDigests")
    for ref in digests:
        if not isinstance(ref, str):
            continue
        try:
            digest = pinned_digest(ref)
            if digest and repository(ref) == repository(image) and (not expected or digest == expected):
                return ref
        except ValueError:
            continue
    raise ValueError("镜像没有与请求仓库及预期 SHA-256 一致的 RepoDigest")


def pull_image(image, source=None, cn=False, offline=False):
    repository(image)
    if offline:
        # A load/import check must not download, resolve tags or switch registries.
        return inspect_digest(image, pinned_digest(image))
    refs, expected, timeout = candidates(image, source, cn or os.environ.get("REMOTECODE_CN") == "1")
    for index, ref in enumerate(refs, 1):
        host = repository(ref).split("/", 1)[0]
        progress(f"尝试容器镜像源 {index}/{len(refs)}: {host}")
        try:
            with activity("Docker 拉取 " + host):
                run(["docker", "pull", ref], timeout=timeout)
            resolved = inspect_digest(ref, expected)
            progress("容器镜像 digest 校验通过: " + resolved.rsplit("@", 1)[1])
            return resolved
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            progress(f"容器镜像源失败: {host}: {type(exc).__name__}；尝试后续源（如有）")
    raise RuntimeError("所有容器镜像源均失败；现有 state/证书/配对保留。检查镜像源配置和 Docker 日志后重跑。")
