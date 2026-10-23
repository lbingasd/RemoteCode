#!/usr/bin/env python3
"""Install ACP adapters and use prerequisite Codex/Claude CLIs in the home runtime.

Codex CLI and Claude CLI must already be installed, configured and callable by
the developer account. Only ACP adapters are installed here. Missing or broken
CLIs are an error; model credentials and login are managed outside this script.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
from common import metadata, read, require, run, write

ADAPTERS = {
    "codex-acp": "@agentclientprotocol/codex-acp",
    "claude-agent-acp": "@agentclientprotocol/claude-agent-acp",
}
CLIS = ("codex", "claude")
# Adapter env var that points each adapter at a specific prerequisite CLI binary.
ADAPTER_ENV = {"codex": "CODEX_PATH", "claude": "CLAUDE_CODE_EXECUTABLE"}


def find_node(root):
    """Keep private/keg-only Node usable after the bootstrap shell has exited."""
    candidates = [shutil.which("node"), root / "tools/node/bin/node",
                  Path("/opt/homebrew/opt/node@22/bin/node"), Path("/usr/local/opt/node@22/bin/node")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                version = subprocess.check_output([str(candidate), "--version"], text=True).strip()
                if int(version.lstrip("v").split(".")[0]) >= 22:
                    return str(candidate)
            except (OSError, ValueError, subprocess.CalledProcessError):
                pass
    return None


def npm_command(node):
    if os.name != "nt":
        bundled = Path(node).resolve().parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
        if bundled.is_file():
            return [node, str(bundled)]
        npm = shutil.which("npm")
        require(npm, "请先安装 Node.js 22 LTS（包含 npm）")
        resolved = Path(npm).resolve()
        return [node, str(resolved)] if resolved.suffix == ".js" else [npm]
    # Invoke npm's JS entry directly: no cmd.exe or batch-file quoting.
    npm = shutil.which("npm.cmd")
    candidates = [Path(node).parent / "node_modules/npm/bin/npm-cli.js"]
    if npm:
        candidates.append(Path(npm).parent / "node_modules/npm/bin/npm-cli.js")
    script = next((p for p in candidates if p.is_file()), None)
    require(script, "找不到 npm-cli.js；请使用标准 Node.js 安装包")
    return [node, str(script)]


def executable(root, node, key):
    package_dir = root / "agents/node_modules" / ADAPTERS[key]
    package = read(package_dir / "package.json")
    bins = package["bin"]
    relative = bins if isinstance(bins, str) else bins.get(key, next(iter(bins.values())))
    target = (package_dir / relative).resolve()
    require(target.is_relative_to(package_dir.resolve()) and target.is_file(), "npm 可执行文件路径异常")
    with target.open("rb") as stream:
        head = stream.read(100)
    if target.suffix in (".js", ".mjs", ".cjs") or head.startswith(b"#!") and b"node" in head.split(b"\n")[0]:
        return [node, str(target)]
    return [str(target)]


def find_cli(key, args):
    """Resolve a prerequisite CLI binary: explicit --*-path first, then PATH, else None."""
    attr = {"codex": "codex_path", "claude": "claude_path"}[key]
    explicit = getattr(args, attr, None)
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        which = shutil.which(key)
        if which:
            candidates.append(Path(which))
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if not candidate.is_file():
            continue
        try:
            subprocess.run([str(candidate), "--version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=30)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        return str(candidate)
    return None


def resolve_clis(args):
    resolved = {key: find_cli(key, args) for key in CLIS}
    missing = [key for key, binary in resolved.items() if not binary]
    require(not missing, "缺少或无法执行前置 CLI: " + ", ".join(missing)
            + "；请先确保本机 Codex CLI 和 Claude CLI 可用，并加入 PATH 或指定 --codex-path/--claude-path。")
    return resolved


def _backup(path):
    backup = Path(str(path) + ".remotecode-backup")
    backup.unlink(missing_ok=True)
    shutil.move(str(path), str(backup))


def adapter_agents(root, node, env, resolved):
    """Build the ACP agent entries, pointing each adapter at its prerequisite CLI when present."""
    agents = {}
    for label, key, cli_key in (("Codex (local ACP)", "codex-acp", "codex"),
                                ("Claude (local ACP)", "claude-agent-acp", "claude")):
        command = executable(root, node, key)
        agent_env = {"PATH": env["PATH"]}
        agent_env[ADAPTER_ENV[cli_key]] = resolved[cli_key]
        agents[label] = {"command": command[0], "args": command[1:], "env": agent_env}
    return agents


def ensure_lock(args, npm, env, agent_dir, deps):
    """Reuse the lock when deps are unchanged; otherwise back up and regenerate."""
    manifest = agent_dir / "package.json"
    lockfile = agent_dir / "package-lock.json"
    if lockfile.is_file() and manifest.is_file():
        declared = set(read(manifest).get("dependencies", {}))
        if declared == set(deps.values()):
            return
        _backup(manifest)
        _backup(lockfile)
        print("CLI/adapter 依赖集变化，重新生成 npm 锁；旧文件已备份。")
    require(not args.offline, "首次安装不能 offline；先在相同系统/架构预热 npm 缓存")
    dependencies = {}
    for key, package in deps.items():
        version = getattr(args, key.replace("-", "_") + "_version")
        require(version == "latest" or re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version), "版本号不合法")
        if version == "latest":
            endpoint = args.registry.rstrip("/") + "/" + urllib.parse.quote(package, safe="@") + "/latest"
            version = metadata(endpoint)["version"]
        require(isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version), "registry 返回了非法版本")
        dependencies[package] = version
    write(manifest, {"name": "remotecode-local-agents", "version": "1.0.0", "private": True, "dependencies": dependencies})
    command = npm + ["install", "--package-lock-only", "--prefix", str(agent_dir),
                     "--registry", args.registry, "--no-audit", "--no-fund"]
    if args.npm_cache:
        command += ["--cache", str(args.npm_cache.resolve())]
    run(command, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "RemoteCode")
    parser.add_argument("--codex-path", type=Path, help="前置 Codex CLI 绝对路径（默认从 PATH 查找）")
    parser.add_argument("--claude-path", type=Path, help="前置 Claude CLI 绝对路径（默认从 PATH 查找）")
    subs = parser.add_subparsers(dest="action", required=True)
    install = subs.add_parser("install")
    install.add_argument("--registry", default="https://registry.npmjs.org")
    install.add_argument("--npm-cache", type=Path)
    install.add_argument("--offline", action="store_true", help="需要此平台完整 npm 缓存和已有 package-lock.json")
    for key in ADAPTERS:
        install.add_argument("--" + key + "-version", default="latest")
    subs.add_parser("settings")
    execute = subs.add_parser("exec")
    execute.add_argument("tool", choices=(*ADAPTERS, *CLIS))
    execute.add_argument("arguments", nargs=argparse.REMAINDER)
    subs.add_parser("uninstall")
    args = parser.parse_args()
    root = args.root.resolve()
    read(root / "local.json")  # require an installed dedicated runtime
    if args.action == "uninstall":
        shutil.rmtree(root / "agents", ignore_errors=True)
        (root / "acp-settings.json").unlink(missing_ok=True)
        print("已删除本项目的 ACP 适配器；本机 CLI、用户配置与项目保留。")
        return
    node = find_node(root)
    require(node, "请先安装 Node.js 22+（建议 22 LTS）")
    major = int(subprocess.check_output([node, "--version"], text=True).strip().lstrip("v").split(".")[0])
    require(major >= 22, "ACP adapter 需要 Node.js >=22")
    resolved = resolve_clis(args)
    agent_dir = root / "agents"
    env = os.environ.copy()
    paths, seen = [str(agent_dir / "node_modules/.bin"), str(Path(node).parent)], set()
    for entry in env.get("PATH", "").split(os.pathsep):
        if entry and Path(entry).is_absolute() and Path(entry).is_dir():
            key = str(Path(entry).resolve())
            if key not in seen:
                paths.append(entry)
                seen.add(key)
    env["PATH"] = os.pathsep.join(dict.fromkeys(paths))
    if args.action == "install":
        require(args.registry.startswith("https://") and not any(c.isspace() for c in args.registry), "npm registry 必须使用 HTTPS")
        npm = npm_command(node)
        env.setdefault("npm_config_fetch_timeout", "90000")
        env.setdefault("npm_config_fetch_retry_mintimeout", "2000")
        env.setdefault("npm_config_fetch_retry_maxtimeout", "10000")
        env.setdefault("npm_config_update_notifier", "false")
        if args.npm_cache:
            env["npm_config_cache"] = str(args.npm_cache.resolve())
        agent_dir.mkdir(parents=True, exist_ok=True)
        ensure_lock(args, npm, env, agent_dir, ADAPTERS)
        command = npm + ["ci", "--prefix", str(agent_dir), "--registry", args.registry, "--no-audit", "--no-fund"]
        if args.npm_cache:
            command += ["--cache", str(args.npm_cache.resolve())]
        if args.offline:
            command += ["--offline"]
        run(command, env=env)
        print("ACP 适配器安装完成，使用已检测到的本机 Codex/Claude CLI。")
    if args.action in ("install", "settings"):
        # Apply in the browser's Remote/Machine Settings JSON, never overwrite existing user settings.
        write(root / "acp-settings.json", {"acp.agents": adapter_agents(root, node, env, resolved),
                                           "acp.autoApprovePermissions": "ask",
                                           "acp.logTraffic": False, "telemetry.telemetryLevel": "off"})
        print(f"把 {root / 'acp-settings.json'} 的设置合并到浏览器 IDE 的远程设置，扩展使用 formulahendry.acp-client。")
    elif args.action == "exec":
        extra = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        if args.tool in CLIS:
            run([resolved[args.tool]] + extra, env=env)
        else:
            run(executable(root, node, args.tool) + extra, env=env)


if __name__ == "__main__":
    main()
