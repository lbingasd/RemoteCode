#!/usr/bin/env python3
"""Resumable, idempotent orchestration for the cloud and home entrypoints."""
import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from common import activity, download, extract, identifier, metadata, progress, read, request, require, run, sha256, write
from local import host_platform, validate_pair
from transfer import segmented_download
from cloud_ports import preflight_port

PACKAGE = Path(__file__).resolve().parent.parent
SCRIPTS = PACKAGE / "scripts"
EXTENSION = "formulahendry.acp-client"


def script(name, *args, **kwargs):
    return run([sys.executable, SCRIPTS / name, *args], **kwargs)


@contextmanager
def deployment_lock(target):
    """OS-held advisory lock: automatically released even after a killed installer."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lockfile = target.parent / ("." + target.name + ".setup.lock")
    # Never unlink a lock file: deleting it can create a second inode/parallel lock.
    with lockfile.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("此部署正在被另一个一键脚本操作；等待它结束后重试") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def dedicated(path):
    target = path.expanduser().resolve()
    require(target not in (Path.home(), PACKAGE, Path.cwd(), Path(target.anchor)), "必须使用独立部署目录，不能使用主目录/项目根目录/磁盘根目录")
    require(not PACKAGE.is_relative_to(target), "部署目录不能包含本工具包")
    return target


def https(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and not any(c.isspace() for c in value), "镜像/registry 必须是无凭据的 HTTPS URL")
    return value


def bundled_asset(item):
    require(Path(item["name"]).name == item["name"], "资源文件名越界")
    bundled = PACKAGE / "assets" / item["name"]
    if bundled.is_file():
        require(sha256(bundled) == item["sha256"].lower(), f"随包资源校验失败，请重新取得可信工具包: {item['name']}")
        return bundled
    return None


def fetch_item(item, cache, args):
    bundled = bundled_asset(item)
    if bundled:
        return bundled
    target = cache / item["name"]
    if args.offline:
        require(target.is_file() and sha256(target) == item["sha256"].lower(), f"离线缓存缺失或校验失败: {target.name}")
    elif args.simple_download:
        download(item["url"], target, item["sha256"], args.mirror)
    else:
        try:
            segmented_download(item["url"], target, item["sha256"], args.mirror, workers=args.workers)
        except RuntimeError:
            print("分段源不可用，尝试普通校验续传；已完成分段保留。", flush=True)
            download(item["url"], target, item["sha256"], args.mirror)
    return target


def select_lock(args, target, platform_name, local=False):
    if local and (target / "download.lock.json").is_file():
        saved = target / "download.lock.json"
        if args.lock:
            require(read(saved) == read(args.lock), "已安装版本与指定 lock 不同；一键重跑不会自动升级，请使用新运行目录")
        return saved
    if args.lock:
        chosen = args.lock.resolve()
    else:
        filename = ("cloud-" if not local else "") + platform_name + ".lock.json"
        chosen = PACKAGE / "examples" / filename
        if not chosen.exists():
            chosen = args.cache / filename
            if not chosen.exists():
                require(not args.offline, "此架构没有随包 lock；离线前用 download.py resolve 生成并通过 --lock 提供")
                with activity("从官方元数据解析并锁定程序版本"):
                    script("download.py", "resolve", "--platform", platform_name, "--lock", chosen)
    lock = read(chosen)
    require(lock["platform"] == platform_name, "下载 lock 与目标系统/架构不一致")
    return chosen


def cloud_setup(args, root):
    if args.action != "install":
        if not (root / "state.json").exists():
            require(not root.exists() or not any(root.iterdir()), "目录非空但没有本项目 state，拒绝操作")
            print("云端尚未安装，或已经卸载。")
            return
        if args.action == "status":
            script("cloud.py", "--state", root, "status")
            script("cloud.py", "--state", root, "list")
        else:
            extra = ["--purge"] if args.purge else []
            script("cloud.py", "--state", root, "uninstall", *extra)
        return
    require(args.ip, "首次安装/重跑云端请提供 --ip 公网IPv4")
    address = ipaddress.ip_address(args.ip)
    require(address.version == 4, "当前入口支持公网 IPv4")
    require(sys.platform.startswith("linux"), "一键云端部署请在 Linux 或 Linux VM 内运行")
    progress("[云端 1/6] 检查 Docker 引擎和目标架构")
    require(shutil.which("docker") and shutil.which("openssl"), "缺少 Docker/OpenSSL；取消 --skip-deps 或预先安装")
    with activity("读取 Docker 引擎信息"):
        info = subprocess.check_output(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], text=True).strip()
    arch = "arm64" if "arm64" in info or "aarch64" in info else "amd64"
    require(info.startswith("linux/"), "需要 Linux 容器")
    statefile = root / "state.json"
    if statefile.is_file():
        progress("[云端 2/6] 复用已安装的 frp 和云端状态")
        state = read(statefile)
        require(state["ip"] == str(address), "公网 IP 与现有部署不同；不会替换证书或现有配对")
        if args.cert or args.key:
            require(args.cert and args.key and args.cert.read_text() == (root / "certs/fullchain.pem").read_text()
                    and args.key.read_text() == (root / "certs/privkey.pem").read_text(), "浏览器证书与已有部署不同；换证书请使用 cloud.py renew-cert")
        if args.lock:
            require(read(args.lock)["frp_version"] == state["frp_version"], "现有 frps 版本与 lock 不同；不会隐式升级")
        preflight_port(root, state)
    else:
        progress("[云端 2/6] 准备 frp 安装包；连接、续传与校验进度如下")
        require(not root.exists() or not any(root.iterdir()), "初始化目录非空，拒绝覆盖")
        preflight_port(root)
        lockfile = select_lock(args, root, "linux-" + arch)
        fetch_item(read(lockfile)["files"]["frp"], args.cache, args)
        extra = ["--image", args.image] if args.image else []
        if args.cert or args.key:
            require(args.cert and args.key, "--cert/--key 必须同时提供")
            extra += ["--cert", args.cert.resolve(), "--key", args.key.resolve()]
        with activity("初始化网关配置和证书"):
            script("cloud.py", "--state", root, "init", "--ip", address,
                   "--lock", lockfile, "--cache", args.cache, *extra)
        state = read(statefile)
    progress("[云端 3/6] 检查/注册设备 " + args.machine)
    if not any(m["id"] == args.machine for m in state["machines"]):
        script("cloud.py", "--state", root, "add", args.machine)
    else:
        print(f"机器 {args.machine} 已注册，保留现有密码和隧道密钥。")
    state = read(statefile)
    progress("[云端 4/6] 检查 nginx 镜像缓存和固定版本")
    # A pinned image already present locally needs neither registry access nor an update.
    image = state["image"]
    present = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if args.image and args.image != state["image"]:
        if "@sha256:" in state["image"]:
            source = state.get("image_source")
            if not source:
                # Compatibility with states created before image_source was recorded.
                raw = subprocess.check_output(["docker", "image", "inspect", args.image, "--format", "{{json .RepoDigests}}"], text=True)
                require(state["image"] in json.loads(raw), "已有镜像已锁定；升级请显式使用 cloud.py pull --image")
            else:
                require(args.image == source, "镜像来源与原部署不同；升级请显式使用 cloud.py pull --image")
    if "@sha256:" not in state["image"] or not present:
        extra = ["--offline"] if args.offline else []
        if args.cn:
            extra += ["--cn"]
        if args.image and "@sha256:" not in state["image"]:
            extra += ["--image", args.image]
        with activity("获取并锁定 nginx 镜像；Docker 输出如下"):
            script("cloud.py", "--state", root, "pull", *extra)
    # Unchanged reruns preserve open sessions. Any externally changed credentials,
    # config/cert/binary force the low-level up, which revokes existing sessions.
    import hashlib
    progress("[云端 5/6] 校验配置并启动网关容器")
    fingerprint = hashlib.sha256()
    for path in sorted([root / "compose.json", *root.joinpath("live").rglob("*"),
                        *root.joinpath("certs").rglob("*"), root / "bin/frps"]):
        if path.is_file():
            fingerprint.update(str(path.relative_to(root)).encode())
            fingerprint.update(bytes.fromhex(sha256(path)))
    digest = fingerprint.hexdigest()
    previous = read(root / "setup.json") if (root / "setup.json").exists() else {}
    with activity("校验 nginx/frps 并启动服务"):
        if previous.get("applied") == digest:
            import cloud
            script("cloud.py", "--state", root, "check")
            preflight_port(root, read(statefile))
            cloud.compose(root, "up", "-d", "--remove-orphans")
        else:
            script("cloud.py", "--state", root, "up")
            write(root / "setup.json", {"applied": digest})
    progress("[云端 6/6] 显示容器状态和配对目录")
    script("cloud.py", "--state", root, "status")
    print(f"云端完成。将 {root / 'enrollment' / args.machine} 安全复制到对应家庭电脑。")
    print(f"iPad 公共根证书: {root / 'pki/root-ca.crt'}；CA 私钥不外传。")
    print("安全组仅需 TCP 443；请自行确认已有管理端口与 IPv6 规则。新增机器会让已有连接短暂重连。")


def node_ok(binary):
    try:
        output = subprocess.check_output([str(binary), "--version"], text=True).strip()
        return int(output.lstrip("v").split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False


def ensure_node(root, args):
    from agents import find_node
    node = find_node(root)
    if node:
        os.environ["PATH"] = str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")
        return node
    private_node = root / "tools/node/bin/node"
    if node_ok(private_node):
        os.environ["PATH"] = str(private_node.parent) + os.pathsep + os.environ.get("PATH", "")
        return str(private_node)
    require(sys.platform.startswith("linux") and not args.skip_deps and not args.offline,
            "缺少 Node.js 22+；请安装后重跑（Linux 联网时可自动安装专用 Node）")
    # Private Node avoids replacing the distribution's Node used by other applications.
    arch = host_platform().split("-")[1].replace("amd64", "x64")
    releases = metadata("https://nodejs.org/dist/index.json")
    version = next(item["version"] for item in releases if item["version"].startswith("v22.") and item.get("lts"))
    name = f"node-{version}-linux-{arch}.tar.xz"
    with request(f"https://nodejs.org/dist/{version}/SHASUMS256.txt") as response:
        checksums = response.read().decode().splitlines()
    matches = [line.split()[0] for line in checksums if line.split()[-1] == name]
    require(len(matches) == 1, "Node 官方校验文件中缺少此架构")
    item = {"name": name, "url": f"https://nodejs.org/dist/{version}/{name}", "sha256": matches[0]}
    archive = fetch_item(item, args.cache, args)
    (root / "tools").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root / "tools") as temp:
        # Node includes npm symlinks. Python's data filter constrains them to this stage.
        with tarfile.open(archive) as source:
            source.extractall(temp, filter="data")
        unpacked = Path(temp) / name.removesuffix(".tar.xz")
        require(node_ok(unpacked / "bin/node"), "Node 包不可运行")
        unpacked.replace(root / "tools/node")
    os.environ["PATH"] = str(private_node.parent) + os.pathsep + os.environ.get("PATH", "")
    return str(private_node)


def agent_identity(root):
    """Skip npm ci only when the lock, interpreter and actual executables are intact."""
    from agents import PACKAGES, executable
    node = shutil.which("node")
    lock = root / "agents/package-lock.json"
    if not lock.is_file() or not node:
        return None
    try:
        for name, package in PACKAGES.items():
            executable(root, node, name)
            require(read(root / "agents/node_modules" / package / "package.json")["version"] ==
                    read(root / "agents/package.json")["dependencies"][package], "版本不完整")
        return {"lock": sha256(lock), "node": str(Path(node).resolve())}
    except (OSError, ValueError, KeyError, StopIteration):
        return None


def merge_settings(root):
    target = root / "data/Machine/settings.json"
    try:
        current = parse_jsonc(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
    except (ValueError, OSError) as exc:
        raise ValueError(f"现有远程设置不能解析，已保留；修复 {target} 后重跑，或手动合并 acp-settings.json 并加 --no-agents") from exc
    require(isinstance(current, dict), "远程 settings.json 必须是对象")
    generated = read(root / "acp-settings.json")
    existing = current.get("acp.agents", {})
    require(isinstance(existing, dict), "现有 acp.agents 必须是对象")
    current["acp.agents"] = {**existing, **generated.pop("acp.agents")}
    for key, value in generated.items():
        current.setdefault(key, value)
    if target.exists() and not target.with_suffix(".json.remotecode-backup").exists():
        shutil.copy2(target, target.with_suffix(".json.remotecode-backup"))
    write(target, current)


def parse_jsonc(source):
    """Remove comments/trailing commas outside strings, then use the strict JSON parser."""
    out, index, quoted = [], 0, False
    while index < len(source):
        char = source[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                out.append(source[index])
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            out.append(char)
            quoted = True
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            require(end >= 0, "未闭合的 JSONC 注释")
            out.append(" ")
            index = end + 2
        else:
            out.append(char)
            index += 1
    text = "".join(out)
    out, index, quoted = [], 0, False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                out.append(text[index])
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif char == ",":
            tail = text[index + 1:].lstrip()
            if not tail.startswith(("}", "]")):
                out.append(char)
        else:
            out.append(char)
        index += 1
    return json.loads("".join(out))


def extension_installed(root):
    for path in (root / "extensions").glob("*/package.json"):
        try:
            package = read(path)
            if f"{package.get('publisher')}.{package.get('name')}" == EXTENSION:
                return True
        except (OSError, ValueError):
            pass
    return False


def install_extension(root, args):
    if extension_installed(root):
        print("ACP 扩展已安装，保留现有版本。")
        return
    if args.vsix:
        require(args.vsix_sha256, "本地 VSIX 需要 --vsix-sha256")
        script("local.py", "--root", root, "install-extension", "--vsix", args.vsix, "--sha256", args.vsix_sha256)
    else:
        item = read(PACKAGE / "examples/acp-extension.lock.json")
        target = bundled_asset(item)
        if target:
            pass
        elif args.offline:
            target = fetch_item(item, args.cache, args)
        else:
            # Marketplace may gzip; decode before hashing with the ordinary downloader.
            target = download(item["url"], args.cache / item["name"], item["sha256"], args.mirror)
        script("local.py", "--root", root, "install-extension", "--vsix", target, "--sha256", item["sha256"])
    require(extension_installed(root), "扩展命令结束但未找到 ACP 扩展，请检查市场网络或提供 VSIX 后重跑")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def windows_service(root, action, args, nssm=None):
    """Only this small service operation is elevated; IDE/npm always run as developer."""
    import base64
    import ctypes
    params = f"-Action {ps_literal(action)} -Root {ps_literal(root)}"
    if nssm:
        params += f" -NssmPath {ps_literal(nssm)} -NssmSha256 {ps_literal(sha256(nssm))}"
    if args.headless:
        params += " -Headless"
    command = "$ErrorActionPreference='Stop'; try { & " + ps_literal(SCRIPTS / "local-windows.ps1") + " " + params + "; exit 0 } catch { Write-Error $_; exit 1 }"
    encoded = base64.b64encode(command.encode("utf-16-le")).decode()
    powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    if action == "Status" or ctypes.windll.shell32.IsUserAnAdmin():
        run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded])
    else:
        outer = "$p=Start-Process -FilePath " + ps_literal(powershell) + " -Verb RunAs -Wait -PassThru -ArgumentList '-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded + "'; exit $p.ExitCode"
        run([powershell, "-NoProfile", "-Command", outer])


def prepare_nssm(root, args):
    if args.nssm_path:
        require(args.nssm_sha256 and sha256(args.nssm_path) == args.nssm_sha256.lower(), "自带 NSSM 缺少可信 SHA-256 或校验失败")
        return args.nssm_path.resolve()
    require(host_platform() == "windows-amd64", "自动 NSSM 仅支持 Windows x64；其他架构请提供经校验的 --nssm-path/--nssm-sha256 或使用 --no-service")
    item = read(PACKAGE / "examples/nssm.lock.json")
    target = root / "tools/nssm.exe"
    record = root / "tools/nssm.json"
    if target.is_file() and record.is_file():
        prior = read(record)
        if prior.get("archive") == item["sha256"] and prior.get("exe") == sha256(target):
            return target
    archive = fetch_item(item, args.cache, args)
    with tempfile.TemporaryDirectory() as temp:
        extract(archive, temp)
        binary, = Path(temp).glob("*/win64/nssm.exe")
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(binary, target)
    write(record, {"archive": item["sha256"], "exe": sha256(target)})
    # Executable identity is derived from a ZIP verified against Microsoft's manifest.
    return target


def home_setup(args, root):
    configfile = root / "local.json"
    if args.action != "install":
        if not configfile.exists():
            require(not root.exists() or not any(root.iterdir()), "目录非空但没有本项目 local.json，拒绝删除")
            print("家庭端尚未安装，或已经卸载。")
            return
        if args.action == "status":
            if not args.no_service:
                if os.name == "nt":
                    windows_service(root, "Status", args)
                else:
                    script("local-service.py", "status", "--root", root)
            script("local.py", "--root", root, "doctor")
        else:
            # Always remove managed services on uninstall, regardless of --no-service.
            if os.name == "nt":
                windows_service(root, "Remove", args)
            else:
                script("local-service.py", "remove", "--root", root)
            extra = ["--purge"] if args.purge else []
            script("local.py", "--root", root, "uninstall", *extra)
            print("共享依赖、下载缓存、项目、.codex/.claude 和厂商登录资料保留。")
        return
    require(args.accept_license, "请阅读微软 VS Code Server 许可，再添加 --accept-license")
    require(args.bundle and args.workspace, "家庭端 install 需要 --bundle 和 --workspace")
    workspace = args.workspace.expanduser().resolve()
    require(not workspace.is_relative_to(root) and not root.is_relative_to(workspace), "工作区与安装目录必须互相独立")
    pair = read(args.bundle / "machine.json")
    validate_pair(pair)
    platform_name = host_platform()
    if configfile.is_file():
        config = read(configfile)
        require(config["platform"] == platform_name, "现有安装系统/架构不同")
        require(config["id"] == pair["id"] and config["frp_version"] == pair["frp_version"]
                and config["workspace"] == str(workspace) and config["url"] == pair["url"], "配对/工作区与现有安装不同；不会覆盖，请用新目录或按运维文档重新配对")
        require((root / "ide-token").read_text().strip() == pair["ide_token"]
                and (root / "ca.pem").read_bytes() == (args.bundle / "ca.pem").read_bytes(), "密钥/CA 已变更；请按运维文档停止服务并重新配对")
        import tomllib
        require(tomllib.loads((root / "frpc.toml").read_text())["auth"]["token"] == pair["frp_token"], "frp token 已变更；请重新配对")
        select_lock(args, root, platform_name, local=True)
        print("家庭运行目录已安装；保留版本、密钥、IDE 数据和项目。")
    else:
        require(not root.exists() or not any(root.iterdir()), "运行目录非空，拒绝覆盖")
        lockfile = select_lock(args, root, platform_name, local=True)
        lock = read(lockfile)
        require(lock["frp_version"] == pair["frp_version"], "frpc/frps 版本不匹配；请提供匹配的 --lock")
        for key in ("frp", "vscode_web"):
            fetch_item(lock["files"][key], args.cache, args)
        workspace.mkdir(parents=True, exist_ok=True)
        script("local.py", "--root", root, "install", "--bundle", args.bundle.resolve(), "--lock", lockfile,
               "--cache", args.cache, "--workspace", workspace, "--accept-license")
    setupfile = root / "setup.json"
    progress = read(setupfile) if setupfile.exists() else {}
    if not args.no_agents:
        require(shutil.which("git"), "缺少 Git，取消 --skip-deps 或先安装 Git")
        ensure_node(root, args)
        identity = agent_identity(root)
        if not identity or identity != progress.get("agents"):
            extra = ["--offline"] if args.offline else []
            script("agents.py", "--root", root, "install", "--registry", args.registry,
                   "--npm-cache", args.cache / "npm", *extra)
            progress["agents"] = agent_identity(root)
            write(setupfile, progress)
        else:
            script("agents.py", "--root", root, "settings")
            print("CLI/ACP 已安装且版本一致，跳过 npm ci。")
        merge_settings(root)
    if not args.no_extension:
        install_extension(root, args)
    if not args.no_service:
        if os.name == "nt":
            nssm = prepare_nssm(root, args)
            windows_service(root, "Install", args, nssm)
        else:
            script("local-service.py", "install", "--root", root)
        # The service managers return before the IDE has opened its port.
        for attempt in range(4):
            check = subprocess.run([sys.executable, str(SCRIPTS / "local.py"), "--root", str(root), "doctor"],
                                   capture_output=True, text=True)
            if check.returncode == 0:
                print(check.stdout, end="")
                break
            if attempt == 3:
                print(check.stdout, end="")
                print(check.stderr, end="", file=sys.stderr)
                raise ValueError("程序已安装，但健康检查未通过；检查云端/服务日志后重跑同一命令")
            print("等待本机 IDE 和云端 TLS 就绪…", flush=True)
            time.sleep(2)
    else:
        print("已跳过自动运行项；用 local.py run-ide / run-frpc 分别启动后再 doctor。")
    script("local.py", "--root", root, "url")
    print(f"初次 token 链接和密码仅在自己终端查看：{sys.executable} {SCRIPTS / 'local.py'} --root {root} url --show-secrets")
    print("接下来：iPad 信任公共 CA；在家庭电脑用 agents.py exec codex -- login / exec claude 登录。")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("role", choices=["cloud", "local"])
    p.add_argument("action", choices=["install", "status", "uninstall"])
    p.add_argument("--cn", action="store_true", help="清华依赖镜像、npmmirror、GitHub 多源及默认 nginx 的国内仓库切换")
    p.add_argument("--skip-deps", action="store_true", help="已有依赖，不调用系统包管理器")
    p.add_argument("--offline", action="store_true", help="不装系统依赖，只用已有下载/npm/Docker缓存；扩展需本地VSIX")
    p.add_argument("--mirror", type=https, help="自己信任的 HTTPS 下载镜像，含一次 {url}")
    p.add_argument("--registry", type=https)
    p.add_argument("--cache", type=Path, default=PACKAGE / "downloads")
    p.add_argument("--lock", type=Path)
    p.add_argument("--workers", type=int, choices=range(1, 17), default=8)
    p.add_argument("--simple-download", action="store_true", help="不支持 Range 的源使用普通续传模式")
    p.add_argument("--purge", action="store_true", help="卸载时删除专用 state/root，保留共享依赖和缓存")
    p.add_argument("--state", type=Path, default=PACKAGE / "state/cloud")
    p.add_argument("--ip")
    p.add_argument("--machine", type=identifier, default="pc1")
    p.add_argument("--image", help="首次安装可使用自己的 nginx 镜像仓库；已有 digest 不自动更新")
    p.add_argument("--cert", type=Path)
    p.add_argument("--key", type=Path)
    p.add_argument("--root", type=Path, default=Path.home() / "RemoteCode")
    p.add_argument("--bundle", type=Path)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--accept-license", action="store_true")
    p.add_argument("--no-agents", action="store_true")
    p.add_argument("--no-extension", action="store_true")
    p.add_argument("--no-service", action="store_true")
    p.add_argument("--headless", action="store_true", help="Windows 开机 IDE 任务，需要账号密码/UAC；Mac 仍需登录")
    p.add_argument("--vsix", type=Path)
    p.add_argument("--vsix-sha256")
    p.add_argument("--nssm-path", type=Path)
    p.add_argument("--nssm-sha256")
    return p


def main():
    require(sys.version_info >= (3, 12), "需要 Python 3.12+")
    args = parser().parse_args()
    if args.cn:
        os.environ["REMOTECODE_CN"] = "1"
    require(not args.purge or args.action == "uninstall", "--purge 仅用于 uninstall")
    if args.mirror:
        require(args.mirror.count("{url}") == 1, "镜像模板须包含一次 {url}")
    args.registry = args.registry or ("https://registry.npmmirror.com" if args.cn else "https://registry.npmjs.org")
    args.cache = args.cache.expanduser().resolve()
    root = dedicated(args.state if args.role == "cloud" else args.root)
    require(not args.cache.is_relative_to(root) and not root.is_relative_to(args.cache), "缓存必须与运行目录互相独立，避免卸载时误删缓存")
    if args.role == "local":
        if os.name == "nt":
            import ctypes
            require(not ctypes.windll.shell32.IsUserAnAdmin() or args.action != "install", "家庭端 install 请用普通开发账号 PowerShell；脚本仅为注册服务自动弹出 UAC")
        else:
            require(os.getuid() != 0, "家庭端请用普通开发用户执行，不要 sudo")
    with deployment_lock(root):
        (cloud_setup if args.role == "cloud" else home_setup)(args, root)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"RemoteCode 未完成: {exc}\n修复原因后重跑相同命令；安装下载会保留可续传片段，不覆盖已有运行目录。", file=sys.stderr)
        sys.exit(1)
