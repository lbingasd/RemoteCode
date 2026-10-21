# 一键部署

先云端，再家庭电脑，最后在 iPad 导入公共证书并登录。所有命令在解压后的工具包根目录执行；`203.0.113.10` 是示例，必须替换成真实公网 IPv4。

## 云服务器：一条安装命令

支持 Ubuntu 22.04+ / Debian 12+，x64 或 ARM64。脚本自动检查 curl、CA、OpenSSL、Docker Engine 和 Compose 插件，补齐缺失依赖并启用 Docker 开机启动。部署程序使用 Python 3.12+；如果系统只有 Python 3.10/3.11，会用旧 Python 下载并校验私有 Python 3.12，安装在工具包的 `.bootstrap/python`。已有可用 Docker 保留，不自动替换冲突的容器运行时。

```bash
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine pc1 --cn
```

### 443 已被 nginx 或其他服务占用

云端入口需要独占宿主机 TCP 443。脚本会在核心程序下载/初始化前、容器启动前检查监听和 Docker 发布端口，提示占用者；同一 state 的已运行 edge 容器允许重跑，不会把它误认为其他部署。脚本不自动停止现有服务，也不更换公网端口。

先用 `sudo ss -ltnp 'sport = :443'` 确认占用者。如果已确认宿主机 nginx 不再承载其他网站/面板，可停止并关闭其开机启动，再重跑原安装命令：

```bash
sudo systemctl disable --now nginx
sudo ss -ltnp 'sport = :443'
```

第二条此时应没有 nginx 的 LISTEN 记录。保留原 state、证书、配对包和下载缓存，直接续装。其他服务仍需要保留时，当前脚本不会自动整合既有入口，不能直接停止它；应先规划统一反代或独立入口 IP。若占用者是其他 Docker 容器，按其项目管理方式处理，不执行全局停止/prune。

宿主机 nginx 的停用由你显式执行，RemoteCode 卸载不会自动恢复它。以后卸载 RemoteCode 并确认 443 空闲后，可用 `sudo systemctl enable --now nginx` 恢复。预检和实际 Docker 绑定之间仍存在时间窗口，最终以 Docker 启动结果为准。

`--cn` 表示选择清华的 apt / Docker CE 软件仓库镜像；不加时使用系统现有 apt 源和 Docker 官方仓库。系统 apt 源不被改写；安装 Docker 时会保留其软件仓库配置供以后维护，不修改 `daemon.json`。执行过程中断后，重跑同一命令即可。

已有 Docker 但缺少 Compose 时，自动补装官方独立 CLI 插件到 `/usr/local/lib/docker/cli-plugins/docker-compose`，不替换 Docker、containerd 或现有容器。插件固定为 Compose 5.6.1（兼容这里使用的 Compose v2 命令接口），x64/ARM64 的官方 SHA-256 记录在 `examples/compose-plugin.lock.json`；支持下载续传、`--cache` 和 `--mirror`。已有可用 Compose 直接复用；已有不可用的同路径文件或符号链接不会被覆盖，需先检查其来源。自有 `DOCKER_CONFIG/cli-plugins` 中的坏插件可能遮挡系统插件，可用 `docker --help` 查看 Invalid Plugins 并处理对应文件。

### 进度、日志与命令输入

如果终端停在单独的 `>` 提示符，通常是在等待多行命令输入完成，脚本还没有启动；可按 `Ctrl+C` 取消这条未完成的输入。`>` 提示符不要手工复制进命令；手工输入的 `>` 是输出重定向操作符。为避免复制续行符，推荐直接运行下面这一整行（替换示例 IP）：

```bash
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine mac1 --state /opt/remotecode-state --cn
```

新版启动后立即显示带时间的 `[RemoteCode ...]` 阶段提示：系统与基础工具 → Python → Docker → 云端 1/6 至 6/6。下载会在连接前先输出任务名称，传输时展示分段百分比、已完成/复用段数及合并校验阶段；Python 下载、证书初始化、镜像拉取和服务校验等长任务每 10 秒显示一次仍在等待和已耗时信息。等待提示不是虚构的下载百分比，最终仍需校验通过。apt、curl 和 Docker 原始输出保留。

需要同时查看和保存日志时，在 Bash 中执行：

```bash
set -o pipefail
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine mac1 --state /opt/remotecode-state --cn 2>&1 | tee deploy-cloud.log
```

Python 输出已禁用缓冲，管道/重定向也能即时看到进展。新增的阶段/等待日志不打印完整命令行、URL 鉴权参数、token 或密码；外部工具保留自身输出。提交排障信息时仅提供最后几行普通错误日志，不公开配对文件或 `url --show-secrets` 的输出。缺少 `--ip` 会在依赖安装前立即报错。

脚本会下载并校验 frp，生成个人 CA 和 IP 证书，注册机器，拉取并锁定 nginx 镜像 digest，检查配置后启动。默认 state 为工具包内的 `state/cloud`；建议长期部署时明确指定稳定的绝对路径：

```bash
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine pc1 \
  --state /opt/remotecode-state --cn
```

**Ubuntu 22.04 不需要升级系统或手动替换 Python。** 私有解释器来自 Astral 的 `python-build-standalone`，版本和官方发布 SHA-256 固定在 `examples/python-bootstrap.lock.json`，支持同一 `--mirror` 和 `--cache` 参数以及分段续传。不会改动 `/usr/bin/python3`、系统默认 Python 或添加第三方 Python PPA；重跑、状态检查和卸载会自动找到已安装的私有解释器。

`--skip-deps` / `--offline` 不会自动下载私有解释器，需先完成一次联网引导或自行提供 Python 3.12+。其他 Linux 发行版预装依赖后加 `--skip-deps`；更旧 Ubuntu/Debian 的 Docker 软件源仍需单独验证。云端 Windows Server / macOS 请使用 Linux VM 执行此入口；本脚本不自动创建虚拟机，也不把 Windows 容器当作替代品。

使用私有解释器后，日常操作继续运行 `deploy-cloud.sh` 即可；若手动调用底层维护脚本，请用 `./.bootstrap/python/bin/python3 scripts/cloud.py ...`，避免又调用系统的旧 Python。私有解释器和下载缓存随普通卸载/`--purge` 保留；确认无部署依赖它后，才自行清理工具包内 `.bootstrap`。

### 国内网络与镜像

`--cn` / Windows `-CN` 现在对 Python、Compose、frp 的公开 GitHub Release 安装包启用多个国内常用加速源，顺序为：

1. `--mirror` 指定的自定义源（如有）。
2. `https://ghfast.top/{url}`。
3. `https://ghproxy.net/{url}`。
4. `https://gh-proxy.com/{url}`。
5. GitHub 官方地址。

连接/传输失败、HTTP 错误、不支持合法 Range 或最终哈希不匹配时，依次尝试后续源；日志显示当前源序号和域名。404/403 等确定性错误和普通下载的哈希错误不重复等待三次。分段下载可复用有效分段，普通下载为每个源保留独立的可续传前缀，重跑无需删除缓存。全部失败后保留可恢复缓存并退出。

编辑 [`examples/download-mirrors.json`](../examples/download-mirrors.json) 的 `github_release` 数组即可增删或调整优先级；`github_repositories` 限定自动转发的公开工具仓库。只使用 HTTPS；预期 SHA-256 始终来自已核实的官方 lock，不从加速源重新获取可信哈希。GitHub API、带查询参数/凭据的原始 URL、其他仓库和微软地址不自动转发；可显式提供自有 `--mirror`。不加 `--cn` 时，仍使用显式自有源及官方源。

独立调用底层下载器时，可用 `REMOTECODE_CN=1 python3 scripts/download.py fetch ...` 启用同一列表；一键入口会自动传递此设置，覆盖私有 Python/Compose 引导及后续下载。加速源按实际线路可能失败，脚本不会关闭 TLS 或跳过哈希校验。

云端 `--cn` 也为默认 `nginx:stable-alpine` 配置容器镜像源切换，顺序为 DaoCloud `m.daocloud.io/docker.io/library/nginx` → 毫秒镜像 `docker.1ms.run/library/nginx` → GH-Proxy `gh-proxy.com/docker/library/nginx` → Docker Hub。路径和每源拉取超时在 `download-mirrors.json` 的 `nginx_registries`、`docker_pull_timeout_seconds`（默认 180 秒）中设置。镜像按同一个官方 digest 拉取，禁止因源切换改成其他版本；索引固定于 `examples/nginx-image.lock.json`，覆盖 Linux x64/ARM64。现有已固定版本在镜像缓存丢失时仍使用旧 digest。

脚本只为本项目选择镜像引用，不修改 Docker 全局 `daemon.json`、DNS 或重启 Docker。已有未完成的 state 可直接重跑原命令，复用 frps、CA 和设备凭据；拉取全部失败时不改 state/Compose/配对资料。拉取成功后记录实际仓库的 digest 引用，后续重跑已有本地镜像时不再次联网。`--offline` 只校验当前引用的本地 RepoDigest，不下载或切源。底层维护命令也可使用 `./.bootstrap/python/bin/python3 scripts/cloud.py --state /opt/remotecode-state pull --cn`。

显式 `--image` 自有仓库仅拉取指定引用，不自动转发至公共加速仓库；`--mirror` 只控制文件下载。Python / Compose / frp / VS Code / Node / NSSM 可指定自有 HTTPS 下载镜像，nginx 可指定自有容器仓库：

```bash
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine pc1 --cn \
  --mirror 'https://你的下载镜像/{url}' \
  --image '你的镜像仓库/nginx:固定标签'
```

这是参数格式示意，镜像地址必须替换。`--mirror` 必须含一次 `{url}`；镜像只提供字节，预期 SHA-256 仍来自官方 lock。国内镜像无法保证任何网络都可达，也不解决模型服务的网络与账号问题。

### 安装完成后取出两份资料

| 位置（相对 state） | 用途 |
|---|---|
| `enrollment/pc1/` | 完整目录安全复制到家庭 pc1，含该机器的配对密钥和初始密码 |
| `pki/root-ca.crt` | 公共根证书，安全传给 iPad |
| `pki/root-ca.key` | CA 私钥，仅云端保存/安全备份，不能发给家庭电脑或 iPad |

安全组最终仅公开所需的 TCP 443。脚本不更改已有安全组、防火墙、SSH/RDP 配置；处理管理端口前先确认厂商控制台可恢复，也检查 IPv6 策略。frps 和家庭 IDE 端口不能公开。

## 家庭 Windows：一条安装命令

将工具包解压到例如 `C:\RemoteCode-Deploy`，将服务器导出的 `pc1` 目录放在其中。在**普通开发账号**的 PowerShell 中执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-local.ps1 -Action Install -Bundle .\pc1 -Workspace "$env:USERPROFILE\Projects" -AcceptLicense -CN
```

只在这个子进程中放行脚本，不永久修改系统执行策略。`-AcceptLicense` 表示已阅读并接受[微软个人 VS Code Server 许可](https://code.visualstudio.com/license/server)。

脚本自动完成：

1. 检查 Python 3.12+、Node.js 22+、Git；缺失时通过微软 WinGet 安装。缺少 WinGet 时尝试微软官方 `Microsoft.WinGet.Client` 修复流程；系统策略不允许时会明确退出，保留已完成步骤。
2. 下载并校验完整 VS Code Web Server 与 frpc；创建工作区；安装到默认 `%USERPROFILE%\RemoteCode`。
3. 在专用目录安装 Codex、Claude 和两个 ACP 适配器，锁定 npm 版本；安装锁定的 ACP 扩展，合并远程 IDE 设置。
4. 优先使用随包 NSSM，按微软 WinGet manifest 中的 ZIP SHA-256 校验，再提取对应 exe；缺少随包资源时自动下载。
5. 仅为注册 NSSM 服务和 IDE 计划任务弹出 UAC；IDE、npm、AI 不以管理员或 LocalSystem 运行。
6. 等待启动并检查本机 IDE 和云端 TLS，输出普通访问 URL。此检查不等于 iPad/实际跨网链路验收。

`-CN` 使 npm 使用 `https://registry.npmmirror.com`，公开工具的 GitHub Release 下载使用上述多源列表。WinGet 的 Python/Node/Git 仍使用其官方 manifest 和来源；有网络限制时先通过可信渠道安装依赖，再加 `-SkipDeps`。自有下载镜像用 `-Mirror 'https://你的镜像/{url}'`，自选 npm 源用 `-Registry https://你的源`。

依赖引导主要面向 Windows 10/11；没有受支持 WinGet/App Installer 的 Windows Server 环境，请先安装 Python/Node/Git，再用 `-SkipDeps`。云端 Windows Server 仍需要 Linux VM，与家庭 Windows 原生运行方式不同。

默认 IDE 在用户登录后启动，frpc 服务随系统启动。需要无人登录启动 IDE 时增加 `-Headless`，交互输入**同一开发账号的密码，不是 Windows Hello PIN**；任务计划程序保存凭据。重跑同一配置保留已有凭据，不再索要密码。把已存在的登录任务改成 Headless，须先移除运行项后重新安装，详见运维文档。磁盘解锁、休眠和系统策略仍会影响远程可达性。

可自带 NSSM：`-NssmPath C:\Tools\nssm.exe -NssmSha256 '<可信的64位SHA256>'`。自动 NSSM 仅支持 Windows x64；其他架构提供经过核实的兼容二进制，或用 `-NoService` 后手动管理进程。

## 家庭 macOS / Linux：一条安装命令

在云端先用 `--machine mac1` 注册，复制对应 `enrollment/mac1/` 到本机工具包目录。**不要 sudo 运行本机入口**：

```bash
bash deploy-local.sh install --bundle ./mac1 --workspace "$HOME/Projects" \
  --accept-license --cn
```

默认运行目录 `$HOME/RemoteCode`。macOS 通过 Homebrew 补齐 Python / Node / Git；缺少 Homebrew 时使用其官方安装脚本。`--cn` 临时传入清华 Homebrew API、bottles 和 Git 仓库镜像配置。若缺少 Apple Command Line Tools，脚本会唤起安装并退出，完成系统安装界面后重跑同一命令。必要的 sudo/系统交互不能由脚本静默绕过。

Linux 的依赖自动安装范围同云端 Ubuntu/Debian。系统 Node 低于 22 时，在 RemoteCode 专用运行目录安装 Node 22 LTS，不替换其他项目的系统 Node。后续 `agents.py` 能定位此私有 Node。

macOS 使用当前用户 LaunchAgents，需登录；Linux 使用 systemd 用户服务，无人登录启动时管理员可按需执行 `loginctl enable-linger 用户名`。脚本不自动改变休眠、FileVault/BitLocker 或账号安全策略。

## 重复执行、续传和离线

**中断后原样重跑安装命令。** 核心程序默认分段续传，保留完整分段和有效子段，合并后必须通过完整 SHA-256；不支持 Range 的下载源自动回退到普通校验续传，也可显式加 `--simple-download` / `-SimpleDownload`。不要在两个进程中共用同一下载缓存进行下载；同一运行目录的一键操作有系统锁保护。

- 已安装运行目录保留版本、密钥、项目和 IDE 数据；配对包/工作区不同会拒绝覆盖，换密钥请按运维文档操作。
- 已完成的 npm 安装、扩展和服务会检查后复用；npm 使用缓存与重试，系统依赖下载由 apt/Homebrew/WinGet 自身管理，不能承诺它们都具备分段级续传。
- 云端相同配置重跑不强制重建现有容器；新增机器、证书/配置/密钥变化会重建并造成短暂重连。不会在重跑时更新已锁定的镜像。
- 远程设置支持 JSON/JSONC；保留原设置和自定义 ACP 项，首次合并备份为 `data/Machine/settings.json.remotecode-backup`。JSONC 会格式化为 JSON，原注释留在备份；非法设置拒绝覆盖。
- `--skip-deps` / `-SkipDeps`：不调用系统包管理器，其余步骤照常。
- `--offline` / `-Offline`：不安装系统依赖，不请求下载源或 registry；需提前准备全部缓存、已有 npm lock 与完整 npm 缓存、云端 Docker RepoDigest。包自己的安装脚本可能额外联网，真正隔离环境需阻断网络并验证，不承诺任意包脚本绝不联网。

默认缓存是工具包中的 `downloads`，可用 `--cache /目录` / `-Cache C:\目录` 指定。运行目录、缓存和工作区必须相互独立。首次没有某平台随包 lock 时会解析官方版本并保存；已有安装使用自身 lock，不随包中的新版本自动升级。

工具包已包含约 1.2 MiB 的 NSSM ZIP 和 ACP VSIX，每次使用仍校验哈希，无需在目标机再次下载；来源和原始许可见 [assets/README.md](../assets/README.md)。其余大型程序、系统依赖、npm 包与 Docker 镜像需要下载或预备缓存。

可在网络好的机器预下载核心程序：

```bash
python3 scripts/download.py fetch --lock examples/windows-amd64.lock.json \
  --cache downloads --segmented --workers 8
```

然后安全复制 lock 和缓存。NSSM 与 ACP 扩展的资源记录在 `examples/nssm.lock.json` 和 `examples/acp-extension.lock.json`，可用 `scripts/download-file.py` 按其中 URL/hash 预下载到对应文件名。扩展也可用 `--vsix /文件 --vsix-sha256 HASH`（Windows 对应 `-Vsix` / `-VsixSha256`）。VSIX 使用普通校验下载以处理市场的 gzip 响应。

npm 缓存位于 `downloads/npm`。离线 AI 安装还需从相同系统/架构的已安装目录复制 `agents/package.json`、`agents/package-lock.json` 及完整缓存；不要跨 Windows/Mac 复制 node_modules。可先以 `--no-agents` 完成运行目录安装，再复制 agents 锁文件、重跑去掉该选项。云端镜像可在联网机器 `docker save`，目标机 `docker load`，必须保留 RepoDigest，再执行一键安装的 `--offline`。

只需要 IDE 时可加 `--no-agents --no-extension`（Windows：`-NoAgents -NoExtension`）。不注册常驻服务加 `--no-service` / `-NoService`，随后分别在终端运行 `local.py run-ide`、`run-frpc`，再 `doctor`。

## AI 登录与 iPad 接入

AI 账号认证和 iPad 证书信任不能通过服务器脚本代办。请在家庭电脑上运行：

```powershell
# Windows
python scripts/agents.py --root "$env:USERPROFILE\RemoteCode" exec codex -- login
python scripts/agents.py --root "$env:USERPROFILE\RemoteCode" exec claude
python scripts/local.py --root "$env:USERPROFILE\RemoteCode" url --show-secrets
```

```bash
# macOS / Linux
python3 scripts/agents.py --root "$HOME/RemoteCode" exec codex -- login
python3 scripts/agents.py --root "$HOME/RemoteCode" exec claude
python3 scripts/local.py --root "$HOME/RemoteCode" url --show-secrets
```

若 Python 没在 PATH，用入口检查到的解释器路径或 Windows `py -3`。只在自己终端查看首次 token 链接和 Basic Auth 密码，不公开转发。优先在家庭电脑完成 OAuth，避免 iPad 的 localhost 回调指向错误设备。

将 `root-ca.crt` 公共证书传给 iPad，安装描述文件，再到“设置 → 通用 → 关于本机 → 证书信任设置”开启完全信任。使用首次 token 链接访问、输入 Basic Auth 后，收藏不含 token 的 `https://IP/m/pc1/`。如果初始化时用 `--cert/--key` 提供浏览器信任的公网 IP 证书，可省略浏览器的私人 CA 信任步骤。

在 IDE 的 ACP 列表选择 **Codex (local ACP)** 或 **Claude (local ACP)**。终端执行 `hostname` 确认是家庭电脑，再验证编辑、ACP 权限确认、蜂窝网络和断线重连。WebView 仍可能请求 `*.vscode-cdn.net`；本机 CLI 默认访问在线模型，不等于本地推理。

## 查看状态、新增机器与卸载

```bash
# 云端（每次使用与安装时相同的 --state）
sudo bash deploy-cloud.sh status --state /opt/remotecode-state
sudo bash deploy-cloud.sh install --ip 203.0.113.10 --machine mac1 --state /opt/remotecode-state --cn
sudo bash deploy-cloud.sh uninstall --state /opt/remotecode-state
# 彻底删除云端密钥、证书、配对包：额外加 --purge

# macOS / Linux
bash deploy-local.sh status
bash deploy-local.sh uninstall
# 彻底删除专用运行目录：额外加 --purge
```

```powershell
# Windows（普通 PowerShell，移除服务时自动弹 UAC）
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-local.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-local.ps1 -Action Uninstall
# 彻底删除专用运行目录：额外加 -Purge
```

卸载默认停用并移除本项目运行项/容器，保留运行目录，便于恢复；加 purge 才删除专用 state/root。重复卸载不会报“尚未安装”，但遇到非本项目目录会拒绝删除。共享 Python、Node、Git、Docker、软件源、下载缓存、项目及 `.codex` / `.claude` 认证资料均保留。不执行全局 Docker prune。iPad CA 和云安全组需自行按需清理。

撤销机器、轮换凭据、证书更新和升级见[维护文档](operations.md)。当前验证范围与未验证平台见[验证记录](verification.md)。
