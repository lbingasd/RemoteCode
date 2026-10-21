"""Check the gateway's exclusive host port without stopping existing services."""
import errno
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess

from common import progress, require

PS_FORMAT = ('{"id":{{json .ID}},"name":{{json .Names}},"ports":{{json .Ports}},'
             '"project":{{json (.Label "com.docker.compose.project")}},'
             '"service":{{json (.Label "com.docker.compose.service")}},'
             '"workdir":{{json (.Label "com.docker.compose.project.working_dir")}}}')


def publishes(ports, port):
    for match in re.finditer(r"(?:^|,\s*)\S+?:(\d+)(?:-(\d+))?->\d+(?:-\d+)?/tcp(?=,|$)", ports):
        if int(match[1]) <= port <= int(match[2] or match[1]):
            return True
    return False


def listeners(port):
    if shutil.which("ss"):
        command = ["ss", "-H", "-ltnp", f"sport = :{port}"]
    elif shutil.which("lsof"):
        command = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]
    else:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if command[0] == "lsof" and result.returncode == 1 and not result.stdout and not result.stderr:
            return ""  # lsof's documented no-match status, not a diagnostic failure.
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def preflight_port(root, state=None, port=443):
    root = Path(root).resolve()
    project = state.get("project", "remotecode") if state is not None else None
    raw = subprocess.check_output(["docker", "ps", "--format", PS_FORMAT], text=True, timeout=15)
    own, foreign = [], []
    for line in raw.splitlines():
        row = json.loads(line)
        if not publishes(row.get("ports", ""), port):
            continue
        same = (project is not None and row.get("project") == project
                and row.get("service") == "edge" and row.get("workdir") == str(root))
        (own if same else foreign).append(row)
    hint = (f"先确认占用服务的用途，再释放 {port} 并重跑原命令。"
            "不会自动停止其他服务或删除 state。")
    if foreign:
        names = ", ".join(str(row.get("name") or row.get("id")) for row in foreign)
        raise ValueError(f"TCP {port} 已由其他 Docker 容器发布: {names}。" + hint)
    if own:
        progress(f"TCP {port} 由当前 state 的 edge 容器使用，允许重复部署")
        return
    detail = listeners(port)
    if detail:
        raise ValueError(f"TCP {port} 已被占用，尚未启动/重建容器。\n{detail}\n{hint}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and detail == "":
            progress(f"TCP {port} 无 LISTEN 记录但内核暂不可绑定，交由 Docker 校验（可能为 TIME_WAIT）")
            return
        if exc.errno in (errno.EACCES, errno.EPERM) and not detail:
            progress(f"当前用户无法绑定探测 TCP {port}，Docker 将在启动时校验；完整预检请用 sudo")
            return
        require(exc.errno == errno.EADDRINUSE or bool(detail), f"无法检查 TCP {port}: {type(exc).__name__}")
        diagnostic = detail or f"请执行 sudo ss -ltnp 'sport = :{port}' 查看占用者。"
        raise ValueError(f"TCP {port} 已被占用，尚未启动/重建容器。\n{diagnostic}\n{hint}") from None
    progress(f"TCP {port} 端口预检通过")
