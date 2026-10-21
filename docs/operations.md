# 维护与卸载

操作会修改自己管理的运行目录和运行项。云端 `--state`、本机 `--root` 一直使用同一个绝对位置；不要在多个终端同时修改同一份云端配置。

日常安装/重复执行优先使用[一键入口](deploy.md)。以下底层命令用于换密钥、证书和升级等维护，不需要手工重做完整安装步骤。一键入口不会自动升级已有版本或轮换已有凭据。

## 新增一台机器

```bash
python3 scripts/cloud.py --state state/cloud add mac1
python3 scripts/cloud.py --state state/cloud up
```

只把 `state/cloud/enrollment/mac1/` 复制到 mac1。按对应系统下载、安装、验证并注册运行项，入口为 `https://IP/m/mac1/`。机器名不需要 DNS 记录，也不需要新开公网端口。

每台机器有独立的浏览器随机密码、IDE token、frp token、frps 实例和唯一允许端口；内部端口在共享网络命名空间只监听 `127.0.0.1`。多机属于**同一个受信任个人**：IP 下的多个路径是同一浏览器 origin，不能靠路径提供互不信任用户之间的隔离。

## 撤销机器与换密钥

```bash
# 撤销：立即停止该机器 frps、删除配对资料，然后应用新路由。
python3 scripts/cloud.py --state state/cloud remove pc1
python3 scripts/cloud.py --state state/cloud up

# 更换全部三类凭据；旧隧道会被停止，需将新配对包安全复制到家里。
python3 scripts/cloud.py --state state/cloud rotate mac1
python3 scripts/cloud.py --state state/cloud up
```

`up` 会重建所有该项目容器，清除旧 WebSocket 连接，其他机器会短暂重连；不是无损热更新。选择没有关键交互操作的时段执行。撤销云端入口后，再卸载本机运行项；若云端可能已失陷，也要更换家庭主机 AI/Git 凭据并检查项目改动。

本机换配对前先停止 IDE/frpc：

```bash
python3 scripts/local-service.py stop --root "$HOME/RemoteCode"
python3 scripts/local.py --root "$HOME/RemoteCode" pair --bundle ./新的mac1配对目录
python3 scripts/local-service.py start --root "$HOME/RemoteCode"
```

Windows 的 NSSM 使用 ProgramData 中独立的隧道配置副本。需要先用 `local-windows.ps1 -Action Remove` 移除旧服务和任务，再执行 `local.py pair`，最后重新 `Install`；否则服务仍然持有旧 token/CA。保留原来的已核实 NSSM 文件供重装使用。安装了 `-Headless` 的任务重装时继续提供该选项。

## 证书与备份

个人叶子证书有效期 365 天；CA 有效期十年。过期前执行：

```bash
python3 scripts/cloud.py --state state/cloud renew-private-cert
```

它保留原 CA，所以家庭 frpc 和 iPad 不需要重装信任。浏览器入口若使用外部证书，此命令只更新内部隧道证书。更换外部浏览器证书：

```bash
python3 scripts/cloud.py --state state/cloud renew-cert --cert /证书路径/fullchain.pem --key /私钥路径/privkey.pem
```

外部证书必须包含对应公网 IP SAN，并由浏览器信任的 CA 签发。此命令检查证书/私钥可加载、IP 匹配以及证书尚未过期；签发链是否被浏览器信任、剩余有效期是否足够、自动续签任务仍需要你验证。个人叶子证书先在临时目录签发、验证，成功后才替换，CA 缺失或签发失败会保留旧证书与私钥。Let's Encrypt IP 证书有效期很短，不能靠偶尔手工更新。

IP 变化会使浏览器证书 SAN 失配，也需要更新所有家庭主机的 `serverAddr` 和访问 URL。本版本没有自动迁移 IP 的命令：在新 state 中用新 IP 初始化、重新配对并在 iPad 更新信任，完成后撤销旧部署。

加密备份云端专用 state，尤其 `state.json`、`pki/` 和 `certs/`。`enrollment/` 含可登录的明文初始密码，完成配对后可以删除该目录并妥善保管密码；再次获取时使用 rotate 生成新凭据。不要把 state 提交到 Git。Windows 的 Python `chmod` 不能代替 NTFS ACL；云端放在管理员专用目录，并按系统 ACL 限制其他用户读取。

## 升级与回滚

不自动滚动升级。使用新的 download lock 记录精确版本和 SHA-256；已有 lock 文件默认拒绝覆盖。frpc/frps 按相同版本成对更新。

为已有 Docker 补装的独立 Compose 插件属于共享依赖，卸载 RemoteCode 时保留。它不会自动更新；后续升级应按 [Docker 官方插件维护说明](https://docs.docker.com/compose/install/linux/)单独安排，并验证 `docker compose version` 和项目 `check`。不要直接覆盖系统中来源不明的插件或删除仍被其他项目使用的插件。

本机 IDE/agent：备份现有安装目录、npm `package-lock.json` 和需要的运行数据；停止旧运行项；使用新的专用安装目录和新 lock 安装，再注册运行项。先验证终端、ACP、认证和断线恢复。失败时停止新运行项，恢复旧目录和旧运行项，不改动项目代码。不要把 Windows、Mac 或不同架构的 `node_modules` 互相复制。

云端：备份 state、原始 lock、frps 和镜像 digest；`down` 后再用新下载的匹配架构 frps 替换 `bin/frps`，更新 state 中 frp 版本，并在家庭端用新 lock 安装及重新配对。此版本没有一键自动迁移状态/多机升级命令；升级前要做完整备份和维护窗口。对于 nginx 镜像，使用 `cloud.py --state state/cloud pull --image nginx:stable-alpine` 重新解析当前标签和锁定 digest，再进行 `check`、`up`；不加 `--image` 时仍检查已固定的旧 digest。

使用 `pull --cn` 时，默认 nginx 的首次版本取自随包 `examples/nginx-image.lock.json`，镜像源列表和超时在 `examples/download-mirrors.json` 中维护。已有固定 digest 的缓存恢复不会改用新版 lock。需要在国内源间升级到另一明确版本时，先从可信来源核实新的官方 digest，再显式提供 `--image docker.io/library/nginx@sha256:完整哈希 --cn`；随后 `check`、`up`。显式自有仓库不自动切换公共源。

AI 包第一次可在相同系统/架构的联网环境预热缓存：

```bash
python3 scripts/agents.py --root "$HOME/RemoteCode" install --npm-cache /路径/npm-cache
# 保留并复制 agents/package.json、package-lock.json 和完整缓存；目标平台必须相同。
python3 scripts/agents.py --root "$HOME/RemoteCode" install --npm-cache /路径/npm-cache --offline
```

某些包的安装脚本可能单独下载原生资源；仅复制 npm cache 不保证这些下载也可离线。若离线安装失败，先在相同平台预装验证，不通过关闭包完整性或 TLS 校验绕过。

## WebView 布局兼容补丁

固定的 VS Code Web 1.143.0 使用 CSS `anchor()` / `anchor-size()` 放置 WebView；实测 Safari 18.5 不支持这些属性，可能出现 agent 已连接、聊天脚本已 ready，面板却位于视口下方的情况。安装器包含一个有版本校验的兼容补丁，支持原生 CSS 锚点时继续使用原实现，否则以元素坐标、ResizeObserver 和滚动/布局变化更新位置、尺寸及裁剪。

已有部署只需更新工具包，在**家庭电脑**的工具包目录执行以下命令；Windows 可将 `python3` 换成已安装的 Python 3.12+ 解释器，将 root 改成自己的安装目录：

```bash
python3 scripts/webview_compat.py apply --root "$HOME/RemoteCode"
```

关闭旧浏览器标签页，重新打开 IDE。补丁更新 workbench 模块 URL 的缓存版本；无需重启服务，不改变 CA、账号、权限或 AI 凭据。全局下载缓存中的官方安装归档不修改。当前补丁只接受已核实的 workbench 完整 SHA-256；升级 VS Code 或其他平台遇到哈希不匹配时会拒绝应用，需要先核实对应文件，不能删除校验强行安装。

原始备份位于 `<root>/server/out/vs/code/browser/workbench/`，文件名为 `workbench.js.remotecode-original` 与 `workbench.html.remotecode-original`。单独回退：

```bash
python3 scripts/webview_compat.py restore --root "$HOME/RemoteCode"
```

回退前也校验当前文件，检测到后续其他修改会拒绝覆盖；备份继续保留。随后关闭旧标签页重新打开。再次运行本机 `install` 会重新应用兼容补丁；普通卸载保留整个运行目录，`--purge` 随专用运行目录一并删除补丁及备份。

## 排障

| 现象 | 优先检查 |
|---|---|
| 浏览器证书错误 | 公网 IP 是否一致、iPad 是否开启个人 CA 完全信任、证书是否过期、设备时间是否正确 |
| 401 | 该机器 Basic Auth 用户/密码；云端重新生成后是否仍使用旧密码 |
| IDE 未授权 | 首次 token URL；浏览器 cookie 是否失效；是否轮换了 IDE token |
| 502 | 家庭电脑是否在线；frpc 认证/重连；IDE 是否监听 127.0.0.1；nginx 对应 loopback 转发端口是否存在 |
| frpc TLS 错误 | 配对 CA、`serverName` 内部标签、云端隧道证书和 IP；不要关闭验证 |
| 网页开了但终端/ACP 不工作 | 检查 WebSocket、扩展版本、适配器依赖、GUI PATH 与绝对路径、CLI 登录、模型服务网络 |
| Claude 已连接、消息长时间转圈 | 检查实际配置的模型服务和认证；Claude 客户端可被配置为 DeepSeek 等兼容服务，连接适配器成功不代表 API Key 有效。实测无效 DeepSeek Key 返回 401 后 CLI 仍会多次重试。继续用 DeepSeek 时，在家庭电脑运行 `python3 scripts/update-deepseek-key.py` 隐藏输入有效 Key，再断开、重连并新建 ACP 会话。 |
| ACP 视图空白或长时间加载 | Safari 18.5 已启动却空白时，先更新工具包并在家庭电脑应用上述布局兼容补丁；其他启动失败仍需检查 `*.vscode-cdn.net`、安全上下文和 Service Worker。完整安装包不代表浏览器资源全部离线可用。 |
| Windows 重启后不可达 | IDE 是否只是登录触发；Headless 账号密码/登录权限；磁盘解锁、休眠；NSSM 服务状态 |
| Mac 注销后不可达 | LaunchAgents 生命周期；FileVault；电源休眠策略 |
| 国内下载中断 | `--cn` / `-CN` 启用公开工具安装包多源切换；源列表在 `examples/download-mirrors.json`，`--mirror` 自有源优先；保留缓存和官方 lock，检查日志中的源序号、域名和 HTTP 状态 |
| nginx 拉取 Docker Hub 超时 | 更新脚本后重跑原 `install --cn`，默认 nginx 会按相同官方 digest 切换国内仓库；检查 `nginx_registries` 和超时配置。不要删除 state 或重新生成配对；`--mirror` 不控制 Docker 镜像 |
| Docker 提示 `443 address already in use` | `sudo ss -ltnp 'sport = :443'` 查看监听者；仅在确认宿主机 nginx 无其他用途后执行 `sudo systemctl disable --now nginx`，再重跑原安装命令。脚本不会代停已有服务；不要删除 state/CA/配对资料 |

云端可执行 `cloud.py status`，或 `docker compose -f state/cloud/compose.json logs --tail 100`。每个新 state 使用独立的 Compose 项目名，启停/卸载只操作自己的项目；旧 state 仍兼容原项目名。本机使用 `local.py doctor`。Windows 可用 `local-windows.ps1 -Action Status`，NSSM 日志在 `ProgramData/RemoteCode/机器名/logs`。Mac 日志在专用 root 的 `logs/`；当前 LaunchAgent 日志不自动轮转，长期运行需按系统日志管理策略轮转。Linux 使用 `journalctl --user -u remotecode-机器名-ide.service`。

## 卸载

一键入口会先移除本项目服务/容器，再按需删除专用目录：

```bash
sudo bash deploy-cloud.sh uninstall --state /opt/remotecode-state --purge
bash deploy-local.sh uninstall --root "$HOME/RemoteCode" --purge
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-local.ps1 -Action Uninstall -Root "$env:USERPROFILE\RemoteCode" -Purge
```

省略 purge 保留运行目录以便恢复；重复执行安全。卸载不安装系统依赖，不删共享包、软件源、下载缓存、项目或原有账号凭据。没有本项目状态文件的非空目录会被拒绝操作。Windows 自动请求同一账号的 UAC，只为移除本项目服务。

需要单独操作底层组件时：

推荐先撤销云端机器，再停止和卸载本机。Mac/Linux：

```bash
python3 scripts/local-service.py remove --root "$HOME/RemoteCode"
python3 scripts/agents.py --root "$HOME/RemoteCode" uninstall
python3 scripts/local.py --root "$HOME/RemoteCode" uninstall --purge
```

Windows（先使用同一开发账号的管理员 PowerShell 移除运行项）：

```powershell
.\scripts\local-windows.ps1 -Action Remove -Root "$env:USERPROFILE\RemoteCode"
python scripts/agents.py --root "$env:USERPROFILE\RemoteCode" uninstall
python scripts/local.py --root "$env:USERPROFILE\RemoteCode" uninstall --purge
```

`local.py` 默认不删除数据；`--purge` 只删除其专用 root，拒绝在 IDE 端口仍被占用时删除。先确认不存在手动前台 frpc，也不要将项目放到安装目录。已有 `.codex`、`.claude`、认证信息、Node、Git 和项目目录不由这个卸载流程删除；需要注销时应另用厂商的 logout 命令。提前备份需要保留的 IDE 设置和会话数据。

整个云端项目卸载：

```bash
python3 scripts/cloud.py --state state/cloud uninstall
# 确认不再需要密钥/CA/配对资料时才彻底删除专用 state：
python3 scripts/cloud.py --state state/cloud uninstall --purge
```

不删除其他 Docker 项目，不自动 prune 全局镜像，不改已有防火墙规则。清理不再需要的云安全组入口和 iPad 的个人 CA 描述文件由你自行操作；先确认没有其他设备/部署仍依赖它。
