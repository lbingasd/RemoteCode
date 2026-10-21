#!/usr/bin/env python3
"""Resolve official release metadata once, then download from a hash-locked manifest."""
import argparse
from pathlib import Path
import re
from common import download, metadata, read, request, require, write
from transfer import segmented_download

PLATFORMS = {
    "windows-amd64": ("windows_amd64", "cli-win32-x64", "server-win32-x64-web"),
    "windows-arm64": ("windows_arm64", "cli-win32-arm64", "server-win32-arm64-web"),
    "darwin-amd64": ("darwin_amd64", "cli-darwin-x64", "server-darwin-web"),
    "darwin-arm64": ("darwin_arm64", "cli-darwin-arm64", "server-darwin-arm64-web"),
    "linux-amd64": ("linux_amd64", "cli-linux-x64", "server-linux-x64-web"),
    "linux-arm64": ("linux_arm64", "cli-linux-arm64", "server-linux-arm64-web"),
}


def resolve(platform, frp_version, vscode_version):
    frp_platform, _cli_platform, server_platform = PLATFORMS[platform]
    require(re.fullmatch(r"\d+\.\d+\.\d+", frp_version), "frp 必须指定明确版本")
    require(vscode_version == "latest" or re.fullmatch(r"\d+\.\d+\.\d+", vscode_version), "VS Code 版本不合法")
    release = metadata(f"https://api.github.com/repos/fatedier/frp/releases/tags/v{frp_version}")
    suffix = "zip" if platform.startswith("windows") else "tar.gz"
    name = f"frp_{frp_version}_{frp_platform}.{suffix}"
    assets = {item["name"]: item for item in release["assets"]}
    require(name in assets, f"此版本未发布 {name}，请检查平台/版本")
    digest = assets[name].get("digest", "") or ""
    if digest.startswith("sha256:"):
        frp_hash = digest.split(":", 1)[1]
    else:
        checksum = assets.get("frp_sha256_checksums.txt")
        require(checksum, "官方未提供 SHA-256，拒绝自动信任镜像")
        with request(checksum["browser_download_url"]) as response:
            lines = response.read().decode().splitlines()
        matches = [line.split()[0] for line in lines if line.split() and line.split()[-1].lstrip("*").split("/")[-1] == name]
        require(len(matches) == 1, "校验文件中找不到唯一资源")
        frp_hash = matches[0]
    version = vscode_version
    # Full native web-server archive includes Node, extensions host and browser UI.
    # No serve-web bootstrap download is required at first browser visit.
    server = metadata(f"https://update.code.visualstudio.com/api/update/{server_platform}/stable/latest") if version == "latest" else metadata(
        f"https://update.code.visualstudio.com/api/versions/{version}/{server_platform}/stable")
    require(server.get("sha256hash") and server.get("url"), "微软元数据未提供 SHA-256；拒绝自动下载")
    require(tuple(map(int, server["name"].split(".")[:3])) >= (1, 128, 1), "拒绝安装存在已知 Web Server 漏洞的版本")
    commit = server["version"]
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "服务器 commit 不合法")
    return {"schema": 1, "platform": platform, "frp_version": frp_version,
            "vscode_version": server["name"], "server_commit": commit,
            "files": {
                "frp": {"name": name, "url": assets[name]["browser_download_url"], "sha256": frp_hash},
                "vscode_web": {"name": f"vscode-web-{server['name']}-{platform}.{'zip' if server['url'].endswith('.zip') else 'tar.gz'}",
                               "url": server["url"], "sha256": server["sha256hash"]}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["resolve", "fetch"])
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--frp-version", default="0.71.1")
    parser.add_argument("--vscode-version", default="latest")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("downloads"))
    parser.add_argument("--mirror", help="自己信任的 HTTPS 镜像模板，例如 https://mirror.example/{url}")
    parser.add_argument("--only-frp", action="store_true", help="云端只需下载 frp")
    parser.add_argument("--segmented", action="store_true", help="Range 分段、跨运行恢复，适合不稳定大文件下载")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk-mib", type=int, default=1)
    args = parser.parse_args()
    if args.action == "resolve":
        require(args.platform, "resolve 需要 --platform")
        require(not args.lock.exists(), "lock 已存在；升级请写入新 lock，避免覆盖回滚基线")
        lock = resolve(args.platform, args.frp_version, args.vscode_version)
        write(args.lock, lock)
        print(f"已锁定 {lock['platform']}: frp {lock['frp_version']}, VS Code {lock['vscode_version']}")
    else:
        lock = read(args.lock)
        for key, item in lock["files"].items():
            if args.only_frp and key != "frp":
                continue
            require(Path(item["name"]).name == item["name"], "缓存文件名越界")
            if args.segmented:
                segmented_download(item["url"], args.cache / item["name"], item["sha256"], args.mirror,
                                   workers=args.workers, chunk_size=args.chunk_mib * 1024 * 1024)
            else:
                download(item["url"], args.cache / item["name"], item["sha256"], args.mirror)
        print(f"缓存校验完成: {args.cache.resolve()}")


if __name__ == "__main__":
    main()
