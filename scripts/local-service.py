#!/usr/bin/env python3
"""Per-user launchd (macOS) / systemd (Linux) lifecycle; never run the IDE as root."""
import argparse
import os
from pathlib import Path
import platform
import plistlib
import subprocess
from common import identifier, read, require, run, write
from local import server_args


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["install", "remove", "start", "stop", "status"])
    p.add_argument("--root", type=Path, default=Path.home() / "RemoteCode")
    args = p.parse_args()
    require(os.name != "nt" and os.getuid() != 0, "请以开发用户运行；Windows 使用 local-windows.ps1")
    root = args.root.resolve()
    config = read(root / "local.json")
    name = identifier(config["id"])
    commands = {"ide": server_args(root, config), "frpc": [str(root / "bin/frpc"), "-c", str(root / "frpc.toml")]}
    if platform.system() == "Darwin":
        parent = Path.home() / "Library/LaunchAgents"
        parent.mkdir(exist_ok=True)
        scope = f"gui/{os.getuid()}"
        targets = [(kind, f"local.remotecode.{name}.{kind}", parent / f"local.remotecode.{name}.{kind}.plist")
                   for kind in commands]
        if args.action == "install":
            # Preflight every existing item before starting/writing either service.
            for kind, _, path in targets:
                if path.exists():
                    require(plistlib.loads(path.read_bytes()).get("ProgramArguments") == commands[kind],
                            f"{path} 属于另一个安装目录，拒绝覆盖")
            created, loaded = [], []
            try:
                for kind, label, path in targets:
                    if path.exists():
                        active = subprocess.run(["launchctl", "print", f"{scope}/{label}"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
                        if not active:
                            run(["launchctl", "bootstrap", scope, path])
                        continue
                    payload = {"Label": label, "ProgramArguments": commands[kind],
                               "WorkingDirectory": config["workspace"] if kind == "ide" else str(root),
                               "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                               "StandardOutPath": str(root / "logs" / f"{kind}.out.log"),
                               "StandardErrorPath": str(root / "logs" / f"{kind}.err.log"),
                               "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}}
                    path.write_bytes(plistlib.dumps(payload))
                    path.chmod(0o600)
                    created.append(path)
                    run(["launchctl", "bootstrap", scope, path])
                    loaded.append(label)
            except BaseException:
                for label in reversed(loaded):
                    subprocess.run(["launchctl", "bootout", f"{scope}/{label}"], check=False)
                for path in created:
                    path.unlink(missing_ok=True)
                raise
            print("已注册当前用户 LaunchAgents；注销/重启后需登录，FileVault 解锁也需要人在现场或既有机制。")
            return
        for kind, command in commands.items():
            label = f"local.remotecode.{name}.{kind}"
            path = parent / f"{label}.plist"
            if path.exists():
                require(plistlib.loads(path.read_bytes()).get("ProgramArguments") == command,
                        f"{path} 属于另一个安装目录，拒绝操作")
            loaded = subprocess.run(["launchctl", "print", f"{scope}/{label}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if args.action in ("remove", "stop"):
                if loaded:
                    run(["launchctl", "bootout", f"{scope}/{label}"])
                if args.action == "remove":
                    path.unlink(missing_ok=True)
            elif args.action == "start":
                require(path.is_file(), "运行项不存在，请先 install")
                if not loaded:
                    run(["launchctl", "bootstrap", scope, path])
            else:
                run(["launchctl", "print", f"{scope}/{label}"])
    else:
        require(platform.system() == "Linux", "仅支持 macOS/Linux")
        parent = Path.home() / ".config/systemd/user"
        parent.mkdir(parents=True, exist_ok=True)
        # Validate both units before any mutation, including remove/start/stop.
        def quote(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        for kind, command in commands.items():
            path = parent / f"remotecode-{name}-{kind}.service"
            if path.exists():
                expected = "ExecStart=" + " ".join(quote(x) for x in command)
                require(expected in path.read_text().splitlines(), f"{path} 属于另一个安装目录，拒绝操作")
        for kind, command in commands.items():
            unit = f"remotecode-{name}-{kind}.service"
            path = parent / unit
            if args.action == "install":
                if path.exists():
                    run(["systemctl", "--user", "enable", "--now", unit])
                    continue
                # systemd is not a shell: double quotes plus escaped backslash/quote/%/$.
                command_line = " ".join(quote(x) for x in command)
                workdir = quote(config["workspace"] if kind == "ide" else root)
                write(path, f"[Unit]\nDescription=RemoteCode {kind}\nAfter=network-online.target\n[Service]\nType=simple\nExecStart={command_line}\nWorkingDirectory={workdir}\nRestart=always\nRestartSec=10\nUMask=0077\n[Install]\nWantedBy=default.target\n")
                run(["systemctl", "--user", "daemon-reload"])
                run(["systemctl", "--user", "enable", "--now", unit])
            elif args.action == "remove":
                subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
                path.unlink(missing_ok=True)
                run(["systemctl", "--user", "daemon-reload"])
            else:
                run(["systemctl", "--user", args.action, unit])
        if args.action == "install":
            print("已注册用户服务；无人登录开机启动需要管理员另行执行 loginctl enable-linger <用户名>。")


if __name__ == "__main__":
    main()
