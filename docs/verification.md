# 来源与验证记录

核对日期：2026-10-21（Asia/Shanghai）。本文件区分方案依据、脚本检查和真实部署证据。

## 上游依据

| 核对内容 | 主要来源 |
|---|---|
| open-remote-ssh 的原生 UI 使用边界，浏览器应直接连接远端 IDE | [维护者关于 code-server 的说明](https://github.com/jeanp413/open-remote-ssh/issues/3) |
| Coder code-server 不发布 Windows 安装包 | [官方安装文档](https://coder.com/docs/code-server/install) |
| VS Code Web Server 的 host、base path、token file、扩展和数据目录参数 | [微软 serverEnvironmentService 源码](https://github.com/microsoft/vscode/blob/main/src/vs/server/node/serverEnvironmentService.ts) |
| 微软 CLI 的原生 Windows/macOS/Linux Web 发行平台 | [微软 update_service 源码](https://github.com/microsoft/vscode/blob/main/cli/src/update_service.rs) |
| frp loopback 绑定与 allowPorts | [官方服务端配置](https://gofrp.org/en/docs/reference/server-configures/) |
| frpc WSS、serverName、trustedCaFile 和认证配置 | [frp 0.71.1 连接实现](https://github.com/fatedier/frp/blob/v0.71.1/client/connector.go)、[该版本 TLS 校验实现](https://github.com/fatedier/frp/blob/v0.71.1/pkg/transport/tls.go) |
| frp WebSocket 固定路径 `/~!frp` | [上游 WebSocket 实现](https://github.com/fatedier/frp/blob/dev/pkg/util/net/websocket.go) |
| ACP 扩展标识、配置格式和适配器 | [formulahendry/vscode-acp](https://github.com/formulahendry/vscode-acp) |
| Codex ACP 包迁移 | [当前上游](https://github.com/agentclientprotocol/codex-acp)、[旧仓库已归档并指向新上游](https://github.com/zed-industries/codex-acp)；npm 安装也实际返回弃用提示 |
| Codex 本机 CLI、Windows 和远程登录 | [官方 CLI 文档](https://learn.chatgpt.com/docs/codex/cli)、[Windows 文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[认证文档](https://learn.chatgpt.com/docs/auth) |
| Claude 原生 Windows/macOS、npm 安装和登录 | [Anthropic 安装说明](https://code.claude.com/docs/en/setup) |
| iPad 手动导入 CA 后仍需开启完全信任 | [Apple 支持文档](https://support.apple.com/en-us/102390) |
| 公网 IP 证书已可申请、有效期短 | [Let's Encrypt 官方公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) |
| Windows Server 不作为 Docker Desktop 支持平台 | [Docker Windows 安装要求](https://docs.docker.com/desktop/setup/install/windows-install/) |

`download.py` 的版本解析已实际读取微软固定版本/完整更新接口和 GitHub 官方 release 元数据。示例 lock 固定 frp 0.71.1 / VS Code 1.143.0。**macOS x64 安装包已经完整下载、校验、安装和运行；Windows/Linux lock 的元数据已核实，但对应系统安装包没有在本次运行。**

## 下载问题及修复

原大文件整流传输多次提前关闭；普通重试只在单次 curl 内续传、没有严格核对返回范围，失败后还会删除已下载片段。已改为可跨运行恢复的普通续传，并新增严格分段模式。分段模式核对 206、范围、总长度、ETag，校验缓存片段，保存中断子段的有效前缀，最后验证完整文件哈希。

当前线路中 Homebrew curl、系统 curl 及请求方式表现不同，不能仅凭这些结果断言某一 TLS 库存在缺陷。实际成功路径是系统 `/usr/bin/curl` + 分段/子段恢复；中断重跑时复用了 50 个已完成分段，最终完成 220 段。

| 已下载程序 | 字节数/校验依据 |
|---|---|
| VS Code Web 1.143.0 macOS x64 | 230,220,150 字节；官方 SHA-256 `0256c257c6386a3e5cfbbe2a7529d80830ead35ceba6cf00d47bd2cfef57b18c` 完全匹配；ZIP 5414 项，实际解压与安装成功 |
| frp 0.71.1 macOS x64 | 官方 release digest 与完整包 SHA-256 匹配 |
| ACP Client 0.2.0 VSIX | 从微软官方扩展市场取得。响应有 HTTP gzip 编码；解码后才是原始 ZIP，记录 SHA-256 `aef8eabe9d311e07c8a34eaaa8c2a965ddb8d06a00f09d651fab54557de55139`；此值是本次官方 HTTPS 下载的内容指纹，不声称是独立发布的签名/校验文件 |
| PowerShell 7.6.7 / Compose 5.6.1 可移植包 | 均与 GitHub 官方 release digest 匹配，仅在临时目录运行 |

VS Code/frp 下载缓存位于当前电脑 `/private/tmp/remotecode-cache`。这是临时缓存，可直接传给本机安装脚本的 `--cache` 参数；系统后续可能清理它，长期保留请自行复制到下载目录。工具包 ZIP 不包含这些大型程序。

## 部署脚本修复

- 完整下载解码 HTTP gzip 后校验原始文件，Range 模式拒绝编码响应；VSIX 安装先检查 ZIP 格式。
- 家庭主机和云端初始化先在临时目录准备，成功后整体发布，拒绝覆盖非空目录；失败回滚，不遗留伪安装状态。
- `doctor` 保存登录跳转 Cookie，并读取实际 frpc 端口；nginx 保留 Host 端口并清理客户端伪造的转发前缀头。
- 云端 state 使用独立 Compose 项目名，避免卸载一个部署时影响另一个；证书签发失败保留旧证书与私钥，外部证书检查 IP/有效期。
- Windows 任务参数改为标准 argv 引号生成，配置按 UTF-8 读取，加入开发账号和服务目录归属检查。
- macOS 运行项安装先整体预检，失败回滚；启停可重复执行，不操作其他安装目录的同名运行项。
- Codex ACP 改为当前维护包，旧锁文件明确提示迁移；安装只请求小型版本元数据，缓存参数覆盖所有 npm 操作，网络超时有界。生成 PATH 移除相对、空及不存在的目录；卸载不再依赖 Node 仍在 PATH。

## 已完成

| 检查 | 结果与范围 |
|---|---|
| 所有 Python 脚本语法编译 | PASS；Python 3.12.8 |
| 针对性自动检查 | 18 项 PASS；涵盖绑定与隔离、安装/初始化回滚、失败续签保留原密钥、下载篡改与截断恢复、编码响应、压缩包越界、Windows 参数引号等直接影响范围 |
| 原生程序及 Compose 配置 | PASS；frp 0.71.1，官方源码临时编译 nginx 1.30.6；实际 nginx 配置/证书检查和独立 Compose 5.6.1 的 `config --quiet` |
| 真实原生链路 | PASS；nginx HTTPS → 每台独立 frps → frpc WSS → 两个真实 VS Code Web Server。检查匿名 401、跨机器密码拒绝、错误 frp token 拒绝、Cookie 路径切换、不可信 CA 拒绝、真实 IDE WebSocket 101、doctor 和访问日志不含 token。证据：[链路报告](evidence/native-stack-macos.json) |
| 浏览器 IDE 与终端 | PASS；当前 Mac 的内置浏览器经 127.0.0.1 HTTP 安全上下文加载真实 IDE，执行 Python 命令并返回 `REMOTECODE_TERMINAL_OK` 与测试目录。此项没有把个人 CA 加入系统信任，浏览器本身未走 HTTPS/frp；上面的 HTTPS/frp 项由独立客户端验证。 |
| ACP 扩展与 WebView | PASS；原始 VSIX 安装成功，扩展激活、两个本机配置项和聊天 WebView 可见。[截图](evidence/browser-terminal-macos.png)。实际观察到 WebView 仍请求 `*.vscode-cdn.net`，完整包不等于所有资源离线可用。 |
| CLI 与 ACP 协议 | PASS；Codex CLI 0.163.0、Claude Code 2.1.306；Codex ACP 2.2.2、Claude ACP 0.94.0 均响应协议 1 的 initialize。使用独立空配置，不登录、不创建模型会话、不发收费模型请求。[协议报告](evidence/acp-initialize-macos.json)、[版本记录](../examples/agents-tested-versions.json) |
| macOS LaunchAgents 生命周期 | PASS；真实 install/status/stop/start/remove，检查 IDE 端口状态和运行项文件，最终无本次测试的持久运行项。[报告](evidence/launchd-macos.json) |
| Windows 脚本解析 | PASS；用官方 PowerShell 7.6.7 在 Mac 解析最终脚本并验证 Native argv 转发；不等于 Windows PowerShell 5.1/NSSM 实际执行通过 |
| 清理 | PASS；测试 IDE/agent 安装已卸载，浏览器测试标签关闭，测试 LaunchAgents 全部移除；未全局安装 nginx/PowerShell/Docker 或修改现有 AI 凭据 |

真实链路的边缘、隧道和 IDE 均为原生真实程序；两个家庭主机角色仍同处当前 Mac 的 loopback，不是真实跨网、多物理机或 Docker 网络环境。早期 `integration_frp.py` 的模拟边缘测试也通过，但不作为替代上述真实栈的证据。

## 未完成的真实验收

| 项目 | 状态及原因 |
|---|---|
| Linux Docker 容器运行 | NOT RUN：无 Docker 守护进程/Linux VM；Compose 离线配置校验和原生 nginx/frp 验证已做，不能代替容器网络与重启验收 |
| Windows PowerShell/NSSM/任务计划 | NOT RUN：无 Windows；仍需真实验证 Windows PowerShell 5.1、ACL、NSSM、登录/无人登录重启两种场景 |
| Linux systemd 安装与卸载 | NOT RUN：无 Linux 宿主；macOS launchd 已实测 |
| Codex/Claude/ACP 登录与真实任务 | NOT RUN：初始化握手和 CLI 查询已通过，但未登录模型账号，也未验证实际对话/编辑任务 |
| iPad Safari、蜂窝网络、WebSocket/webview | NOT RUN：没有接入真实 iPad 和用户云服务器 |
| 真实跨网与长期任务恢复 | NOT RUN：本机双路径、错误凭据和 WebSocket 已验证；iPad 后台挂起、家庭断网、设备休眠、系统重启后的完整恢复仍需目标环境验收 |

因此可以称为“当前 Mac 的真实原生栈与浏览器已验证”，不能称为“家庭 Windows/iPad 已部署完成”或“生产跨网全链路验收通过”。

## 复现检查

```bash
python3 -m py_compile scripts/*.py tests/*.py
python3 -m unittest discover -s tests -v

# 已按官方 SHA-256 校验并解压对应平台 frp 后：
python3 tests/integration_frp.py --frp-dir /绝对路径/frp_版本_平台

# 有当前平台的真实 nginx、frp、已校验完整 IDE 包时：
python3 tests/integration_stack.py --nginx /nginx路径 --nginx-prefix /nginx前缀 --frp-dir /frp目录 --lock examples/darwin-amd64.lock.json --cache /下载缓存 --report /报告路径.json
```

集成测试只监听 127.0.0.1 临时端口，退出时关闭测试服务和子进程、移除测试运行目录。它会使用微软 Server 包，运行者应先阅读相应许可。真实云端随后还必须执行 `cloud.py check`、`up`，以及部署文档末尾的 iPad 验收步骤。

## 一键入口新增验证（2026-10-21）

新增 `deploy-cloud.sh`、`deploy-local.sh`、`deploy-local.ps1`，以现有安装器为底层；`scripts/setup.py` 负责版本/配对检查、阶段恢复、镜像、服务和卸载。既有手动维护命令仍可用。

依赖引导参考 [Docker Ubuntu 官方 apt 安装](https://docs.docker.com/engine/install/ubuntu/)、[Debian 官方说明](https://docs.docker.com/engine/install/debian/)、[清华 Homebrew 镜像说明](https://mirrors.tuna.tsinghua.edu.cn/help/homebrew/)、[微软 WinGet 引导说明](https://learn.microsoft.com/en-us/windows/package-manager/winget/)。NSSM 归档哈希来自[微软 WinGet manifest](https://github.com/microsoft/winget-pkgs/blob/master/manifests/n/NSSM/NSSM/2.24-101-g897c7ad/NSSM.NSSM.installer.yaml)。

| 项目 | 证据与边界 |
|---|---|
| 针对性自动检查 | **34 项 PASS**，含原 18 项；新增互斥锁、离线/非安装不调用包管理器、Range 回退、NSSM 随包校验与 x64 PE 结构、篡改资源拒绝、JSONC 合并与备份、版本保留、云端重跑不换密钥/不强制重建、配置变化后重新应用、重复卸载、运行项归属和私有 Node/npm 定位 |
| Mac 一键实际安装 | PASS：真实完整 IDE、随包 ACP VSIX、缓存 npm ci；从已生成 npm lock 的未完成阶段继续安装，重跑不再次执行 npm ci；保留用户设置和凭据。[完整报告](evidence/oneclick-macos.json) |
| 最终随包资源与自动运行项 | PASS：最终入口优先使用随包 VSIX，真实 install、相同命令重跑、LaunchAgents 重跑、IDE HTTP 200、doctor、变更配对拒绝、purge 及重复卸载。[最终资源报告](evidence/oneclick-bundled-macos.json) |
| doctor 的测试条件 | 本次一键 smoke 使用 loopback 临时 TLS 服务和真实 IDE，仅验证安装器健康检查。真实 nginx/frp 双机链路另见既有报告；不把临时 TLS 服务当作云端 frp 验收 |
| Windows 入口静态检查 | PASS：PowerShell 7.6.7 在 Mac 解析两个最终脚本，5 个 Windows CRT 参数引号用例通过；不等于 Windows PowerShell 5.1 / UAC / NSSM / Task Scheduler 实际运行通过 |
| NSSM 与扩展资源 | NSSM ZIP 实际下载，与微软 manifest 的 SHA-256 `99f5045fffbffb745d67fe3a065a953c4a3d9c253b868892d9b685b0ee7d07b8` 一致；源目录、x64 PE 已检查，未执行 Windows exe。ACP VSIX 使用前述官方市场下载指纹；两个小资源保留原始源码/许可文件随包提供 |
| 系统依赖自动安装 | **NOT RUN**：未在当前共享 Mac 上安装 Homebrew/全局包，未实际执行 apt 或 WinGet。验证了入口语法、退出边界和离线/卸载不安装依赖；Linux/Windows 目标机仍需验收包管理器与系统权限 |
| 清理 | PASS：测试专用运行目录和全部唯一命名 LaunchAgents 移除，项目目录在 purge 后仍保留；未登录 AI 账号，无模型请求 |

NSSM 上游在当前线路的个别 GET 请求曾返回错误，正常 curl GET 随后成功；Jellyfin 官方软件仓库取得的同名 ZIP 也与上述独立 manifest 哈希完全相同。安装包现内置该已核实归档，目标机优先校验随包文件，不依赖临时可达性；不自动切换随机公共代理。

首次 smoke 的 npm 锁文件经 `/var` 路径生成、随后以其 `/private/var` 真实路径使用，导致 npm 认定依赖不一致；已将测试路径在生成锁之前统一规范化。正式入口的运行目录原本就使用 `Path.resolve()`；修正测试夹具后缓存恢复和重跑通过。

最终依赖预检发现 Node 22 拒绝将布尔值作为 `process.exit` 的退出码，导致已有 Node 被误判为不可用；两个入口现均使用整数 `0/1`。自动检查执行了 Windows 入口中的实际 Node 表达式，并验证完整 Mac 工具链不调用 Homebrew；Linux apt 也只安装缺失依赖，不因重跑自动升级已有共享包。

复现新增检查：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n deploy-cloud.sh deploy-local.sh scripts/bootstrap-posix.sh
pwsh -NoProfile -File tests/check-powershell.ps1

# 当前 Mac x64，使用已校验缓存。会短暂创建并最终删除唯一测试 LaunchAgents。
python3 tests/integration_setup.py --cache /缓存目录 --vsix /已核实ACP.vsix \
  --npm-cache /相同平台npm缓存 --with-services --accept-license --report /报告路径.json

python3 scripts/build-package.py
```

一键脚本没有代办 iPad 证书信任、AI 账号登录、云安全组或休眠策略。Windows/Linux/iPad 和真实跨网未完成项仍按上表与前文记录保留，不能将 Mac smoke 提升为这些目标环境已部署的证据。

## Ubuntu 22.04 的 Python 引导修复（2026-10-21）

用户在 Ubuntu 22.04 64 位部署时遇到 `This OS does not ship Python 3.12+`。旧入口按发行版版本直接退出，未给系统 Python 3.10 提供引导路径。现改为保留系统 Python，通过可由 Python 3.8+ 运行的引导器安装私有 Python 3.12.15；不使用 Python PPA，不改系统默认解释器。发行包来自 [Astral 官方项目](https://github.com/astral-sh/python-build-standalone/releases/tag/20261020)，这是 [uv 使用的同类独立 Python 发行方式](https://docs.astral.sh/uv/concepts/python-versions/)。

本次按改动范围执行 **9 项定向检查通过**（7 项私有 Python 引导、2 项现有入口检查），未重跑整个原生链路或所有历史测试。检查涵盖旧解释器回退、镜像/缓存参数转发、私有解释器查找、重复执行复用、失败不发布半成品、归档越界/特殊文件/链接拒绝，以及 Python 3.8 语法兼容。实际下载了约 34.5 MB 的 Linux x64 Python 归档，SHA-256 与官方 release digest 一致，使用 Python 3.11 校验所有归档成员与链接及 ELF 头。[记录](evidence/python-bootstrap.json)

当前宿主是 Mac，Linux 归档的大小写敏感 terminfo 文件不能按原样在本机默认文件系统完整解压；安全解压逻辑用独立夹具验证，真实归档完成全部成员预检。未在 Mac 执行 Linux 二进制，也未执行 Ubuntu 的 apt/Docker，不能把本次检查表述为 Ubuntu 22.04 已实际部署通过。目标服务器更新仓库后重跑原安装命令验证。

```bash
python3 -m unittest discover -s tests -p 'test_python_bootstrap.py'
python3 -m unittest discover -s tests -p 'test_bootstrap.py'
bash -n scripts/bootstrap-posix.sh
```

## 部署进度与等待输出修复（2026-10-21）

原入口没有统一的阶段提示，下载在首次 Range 响应前也没有输出，完成分段通常累计到 20 段才显示；Python 输出进入管道时还可能被缓冲。现增加启动/依赖日志、云端六个阶段、网络/镜像/证书/服务操作的每 10 秒等待提示、按时间节流的分段百分比、缓存复用与完整文件合并校验提示。Python 入口和子进程禁用缓冲，日志可即时通过 `2>&1 | tee` 保存。

补充区分终端的 `>` 续行提示与手动输入的重定向操作符；缺少 `--ip` 时在系统依赖安装前退出。Mac Bash 3.2 的日志变量使用花括号，避免中文标点紧邻变量时被错误解析。

本次 **38 项定向检查通过**：4 项新进度检查、10 项传输检查、14 项部署编排检查、1 项下载完整性检查、2 项入口检查、7 项旧 Python 引导检查。另通过 PowerShell 解析及 5 个 Windows 参数引号用例；使用 Python 3.11 实际演示等待心跳和已校验 Linux Python 缓存复用。未重跑无关的完整原生链路、未连接用户云端或执行 Ubuntu/Docker 部署。[记录](evidence/progress-checks.json)

新增等待日志不打印完整命令行、下载 URL 中的鉴权内容或异常中的潜在秘密；异常路径会停止心跳并显示“未完成”，不会显示成功。源有完整性校验失败时仍拒绝安装，不因增加进度展示改变校验/续传规则。

## 已有 Docker 缺少 Compose 的补装修复（2026-10-21）

旧入口遇到已有 Docker 缺少 Compose 时直接退出。现在按 [Docker 官方独立插件安装方式](https://docs.docker.com/compose/install/linux/)补装到 `/usr/local/lib/docker/cli-plugins`，仅在 `docker compose version` 不可用时执行，不替换现有 Docker 软件包。使用 [Compose 5.6.1 官方发布](https://github.com/docker/compose/releases/tag/v5.6.1)中 x64/ARM64 的独立 digest 锁定下载；先完整校验、暂存并验证插件元数据，再发布可执行文件。已存在的未知文件或符号链接不会被覆盖，共享插件卸载时保留，升级需单独维护。

本次 **30 项定向检查通过**：7 项新增 Compose 引导检查、2 项入口检查、7 项 Python 引导检查、10 项传输检查、4 项进度检查。覆盖已有 Docker 补装及重跑不调用 apt、镜像/缓存参数转发、篡改/元数据错误拒绝、失败不发布、保留未知插件/链接，以及 Range 失败回退。

实际下载 Linux x64 插件 32,676,477 字节，SHA-256 与官方发布 digest 一致，检查 ELF64/x86-64 文件头；ARM64 完成官方 digest 元数据核对。使用已有且重新核对官方 digest 的 Mac 同版本程序，在临时目录实际验证暂存安装、插件元数据、重复执行复用与清理。**未执行 Linux 二进制、apt、Docker 守护进程或连接用户服务器**，目标 Ubuntu 的插件发现、引擎版本兼容及容器运行仍需重跑部署验收。[记录](evidence/compose-bootstrap.json)

```bash
python3 -m unittest discover -s tests -p 'test_compose_bootstrap.py' -v
bash -n deploy-cloud.sh deploy-local.sh scripts/bootstrap-posix.sh
```

## 多个国内常用下载源与失败切换（2026-10-21）

针对云端 GitHub 安装包下载失败，`--cn` / Windows `-CN` 现对指定公开工具的 Release 安装包启用三个 HTTPS 加速源：[`ghfast.top`](https://ghfast.top/)、[`ghproxy.net`](https://ghproxy.net/)、[`gh-proxy.com`](https://gh-proxy.com/)，显式 `--mirror` 优先，官方源兜底。列表和工具仓库范围在 `examples/download-mirrors.json`。三个服务的实际可达性依赖目标机线路；这里仅把它们用于已有官方 SHA-256 的公开安装包字节，不将其作为可信版本或哈希来源。

普通及分段下载共用源顺序，日志显示源序号、域名和错误码；403/404 等确定性错误直接切换，校验失败清除不可信数据后继续。普通下载为不同源分别保留前缀，分段下载仍以完整官方哈希最终确认所有分段。GitHub API、未列入配置的仓库、带查询参数/凭据的 URL 不自动送往这些源。

本次 **54 项定向检查通过**：10 项新多源检查、7 项 Compose 引导、2 项入口、7 项 Python 引导、10 项传输、4 项进度、14 项编排。覆盖源优先级与去重、官方回退、404/403 与 Range 不支持切换、哈希错误后恢复、跨完整失败周期保留各源前缀、日志脱敏，以及 CN 设置传到 shell 引导及 Python 子进程。另通过 Python 3.8 引导语法检查、Bash 语法检查、PowerShell 解析和 5 项 Windows 参数引号检查。未重跑无关的完整原生 IDE/frp 链路，未连接用户服务器或执行 Linux Docker。

三个源对公开 Compose 安装包的 `bytes=0-0` 探测均成功。随后使用空临时缓存实际下载 Linux x64 Compose：先以不可连接的本地自定义源验证首源失败，再在 `ghfast.top` 传输遇到 HTTP 502 后自动切到 `ghproxy.net`，复用 12 个有效分段，最终完成 32,676,477 字节并通过官方 SHA-256。当前 Mac 线路共耗时约 303 秒，不代表用户服务器的速度或可达性；第三个源仅完成 Range 探测，未完整下载。没有执行 Linux 程序。[记录](evidence/mirror-failover.json)

```bash
python3 -m unittest discover -s tests -p 'test_mirrors.py' -v
```
