# 本机 QQ Bot 与 ACP 审批

QQ Bot、截图发送与 ACP 代理运行在家庭主机，复用 Node.js 22 和 Python 3.12，不安装额外 npm/Python 依赖。家庭主机主动连接 QQ WebSocket 和 HTTPS；现有云端 nginx/frp 不改动，不新增公网入站。首版每台机器使用独立 Bot，不支持同一 AppID 被多台机器同时运行。

## 安装与输入 QQ 信息

前置条件：同一开发用户的 Codex CLI 和 Claude CLI 已可用，家庭端默认 AI 部署已完成，IDE 设置中存在 `Codex (local ACP)` 与 `Claude (local ACP)`。部署脚本仅安装 ACP 适配器与 QQ 组件，不安装 CLI 或配置模型；缺少 CLI 时家庭端 AI 部署直接报错。

当前支持 Codex 和 Claude 两类 ACP 会话的一次性权限审批与已授权截图发送，分别通过对应的 `QQ approvals` 条目使用。QQ 不提供新建模型会话、发送任意任务或接管独立终端/桌面 App 会话的功能，其他 agent 类型尚未自动接入。模型与服务商沿用本机 CLI 的配置。

已经安装 RemoteCode 的 macOS/Linux，在工具包目录运行：

```bash
bash deploy-qq.sh install --root "$HOME/RemoteCode"
```

Windows 普通开发用户 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-qq.ps1 -Action install
```

首次完整安装也可在原来的 `deploy-local.sh install` 命令添加 `--qq-bot`，Windows 对应 `-QQBot`。默认不启用 QQ，保留原有部署行为。`--no-service` 只安装组件，不提示凭据或启动 QQ。离线部署不启用 QQ，等联网后运行上述附加安装命令。

脚本依次询问 QQ 平台的 AppID、AppSecret 和是否使用沙箱。AppSecret 用隐藏输入读取，必须使用自己的交互终端，不通过聊天、命令行参数或明文管道传入。通过 QQ 官方鉴权验证后才替换已有配置；正式环境使用官方 `https://api.bot.qq.com`，沙箱使用 `https://sandbox.api.sgroup.qq.com`。机器人自身须有单聊、主动消息和富媒体权限，用户须添加机器人并允许主动消息；鉴权成功不代表这些权限均已获批。

如果组件已部署但尚未配置：

```bash
bash deploy-qq.sh configure --root "$HOME/RemoteCode"
```

脚本启动/重启 QQ 服务，然后显示一个 5 分钟有效的接收用户配对码。用接收通知的 QQ 账号**私聊机器人**发送 `绑定 配对码`。这是用户绑定步骤，区别于 AppSecret 机器人认证。不会把第一个发消息的任意 QQ 用户自动绑定。更换 AppID 或切换沙箱后需重新绑定。

必须在 QQ 中与机器人一对一聊天；群内 @ 或频道私信不进入单聊绑定逻辑。推荐发送完整命令 `绑定 ABCDEF123456`，也支持只发当前有效的配对码。命令兼容小写码、中文冒号、`/` 前缀、单聊中的 @ 提及及常见复制产生的不可见字符/全角字符。只接受与本机当前 5 分钟挑战完全匹配的码，不从任意句子中提取代码。新版将有效挑战存入权限受限的 `qq/pairing.json`，服务重启后在原期限内仍可使用，绑定/解绑后删除；从尚未持久化挑战的旧版本升级需重新运行一次 `pair`，无需重填 AppSecret。

绑定无回复时，`status` 的 `online` 仅表示网关已 READY，不证明消息已经送达。查看 `event_counts.C2C_MESSAGE_CREATE` 和 `last_message.result`：没有单聊事件时检查是否向正确 Bot/环境发送，以及发送账号的平台测试/使用权限；`pair_code_mismatch`、`pair_expired`、`pair_format_invalid` 分别表示配对码不匹配、过期或格式错误，`unbound_sender` 表示消息已收到但没有识别为绑定命令。状态不输出配对码、QQ 用户标识和消息正文。配对命令启动立即输出，每 5 秒显示已等待时间、单聊计数和最近处理结果，避免非绑定消息分支静默等待。

绑定记录写入和 QQ 确认回复分开记录；只有确认回复发送成功才显示“确认回复已发送”，失败时在终端显示数值错误码。绑定成功但手机未收到时，可用 `status` 的 `bound`、`last_message.result=bound_reply_failed` 和 `last_error` 区分接收成功与发送失败。

## Windows 无窗口运行与自动恢复

QQ 任务由 `pythonw.exe` 启动，不再使用带控制台的 PowerShell runner。IDE 与 QQ 的源码共用 Windows 守护模块，分别使用独立的锁、状态文件和日志；不会因 QQ 重启而终止 IDE。Bot 进程正常或异常退出后均约 5 秒重启；守护进程退出后，每分钟的无限期任务触发会恢复运行，`IgnoreNew` 和文件锁防止重复实例。子进程树随守护进程结束清理，避免旧进程占用本机接口。

已有部署在更新工具包后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-qq.ps1 -Action install -NoConfigure
```

该命令更新组件并迁移原有受管理的 QQ 任务，保留 AppID/AppSecret、绑定和本机 token，不要求重新输入 Secret 或重新绑定。自定义运行目录仍须添加同一 `-Root`。任务动作或用户被自行修改时拒绝覆盖。

主动执行 `-Action stop` 会先禁用任务，随后停止守护与 Bot；不会被每分钟触发器拉起。`start` / `restart` 重新启用；`uninstall` 移除任务。只在任务管理器中结束 Bot 或守护进程则会自动恢复。默认需要开发用户登录，注销、关机、休眠期间不保证在线；待审批请求不会因恢复而重新生效，需按原审批流程重试。

生命周期日志为 `<Root>\logs\qq-supervisor.log`，只保存时间、PID、启动/退出码和重试；不记录 Bot stdout/stderr、QQ 消息或凭据。网关是否 READY 及诊断错误码用 `status` 查看。

## 在 ACP 中使用

刷新浏览器 IDE，断开原 Codex 或 Claude 连接，选择新增的 **Codex (QQ approvals)** 或 **Claude (QQ approvals)**。它们分别代理现有 `@agentclientprotocol/codex-acp` / `@agentclientprotocol/claude-agent-acp` 的 stdio 连接，不另建模型会话；原 **Codex (local ACP)** 和 **Claude (local ACP)** 条目保留。

Bot 在线且用户已绑定时，新增条目的 `session/request_permission` 请求交给 QQ。首版采用单一审批通道：该请求不会同时弹出 IDE 选择框。通知优先使用 QQ markdown，按标题、审批码、会话、详情分层排版（详情按原始输入键值结构化展示，而非 JSON 原文），并附「同意一次 / 拒绝一次」回调按钮，点一下即可审批。点击按钮触发 `INTERACTION_CREATE`，Bot 在约 3 秒内回执 `PUT /interactions/{id}`，并按按钮携带的审批码精确匹配，安全边界与文字命令一致。markdown/按钮不可用（如沙箱）或发送失败时自动降级为纯文本通知。消息不再附带网页 IDE 链接。支持如下指令：

```text
1 同意一次
2 拒绝一次
同意 12位审批码
拒绝 12位审批码
同意
拒绝
查看截图 12位截图或审批码
查看截图
状态
帮助
```

`1`/`2` 和裸 `同意`/`拒绝` 只在恰好有一条待审批请求时作用于它；零条提示无待处理，多条提示改用 `同意/拒绝 审批码` 精确指定，避免静默审批错误的请求。显式 `同意/拒绝 审批码` 始终按码精确匹配。按钮点击等价于显式 `同意/拒绝 审批码`，始终按按钮携带的审批码精确匹配；已处理、已取消或非绑定用户点击只回执相应错误码，不重复执行。命令兼容 `同意：审批码` 的全角/半角冒号与常见复制产生的全角字符。

仅选择请求中实际提供的 `allow_once` / `reject_once` 选项，不支持永久授权，也不把自由文本交给模型判断是否同意。原请求没有对应的一次性选项时取消。操作摘要可能截断，信息不足时拒绝并在 IDE 中核实。通知只遮盖已知机器人凭据和常见密钥字段，不能保证识别任意命令中的秘密；敏感操作请使用原 Codex/Claude 条目。

服务不可用、未绑定或推送失败时，原请求回到 IDE。已经交给 QQ 后发生服务故障、5 分钟超时或 ACP 会话取消，则取消该请求，不能静默执行；需重试操作。代理持续轮询续租，失联超过 15 秒的待审批请求失效。重复消息、迟到回复及已处理编号不再次执行；重启不恢复待审批请求。

QQ 的“决定已记录”表示等待 ACP 领取，不代表操作已成功执行；执行结果以 IDE 为准。原 Codex/Claude 沙箱/策略仍生效，代理只能处理 adapter 实际发出的 ACP 审批，无法接管其他 App、CLI 会话或平台审批。

## 截图 skill

安装器将 `remotecode-qq` skill 安装到当前用户的 Codex 与 Claude skills 目录。新会话可请求“将当前任务截图发送到 QQ”。skill 调用本机受认证接口发送 PNG/JPEG，单张最多 10 MiB、内存最多保留 30 张、24 小时后清理，服务重启后清空；QQ 中的副本由平台保存。截图只发送图片本身，不附带文字或按钮。

也可以在本机手动发送已获授权的截图：

```bash
python3 scripts/qq_bot.py send-image --root "$HOME/RemoteCode" --path /absolute/path/screenshot.png
```

知道真实 ACP session ID 时可添加 `--session ID`，之后 `查看截图 审批编号` 返回该会话最后上传的截图。否则用发送成功后给出的截图编号查看。`查看截图` 返回最后上传的图片。此功能查看已有图片，**不会自动截取系统桌面**；新截图需要会话中已有的截图工具及对应系统权限。

截图上传采用 QQ 官方预上传、分片 PUT、完成确认、合并和富媒体发送流程，无需公开图床。上传到 QQ 的图片会经过腾讯平台；本地清理不撤回 QQ 副本。

上传时按服务端返回的分片序号逐片确认，兼容从 0 或 1 开始的数字/数字字符串。当前真实 QQ 接口实测从 1 开始；不能仅凭文档示例假定从 0 开始。`status.upload_diagnostic` 只包含上传阶段、字段名称、分片序号和长度，不包含预签名地址、上传令牌或图片数据。

## 运维和存储

```bash
bash deploy-qq.sh status
bash deploy-qq.sh restart
bash deploy-qq.sh stop
bash deploy-qq.sh start
bash deploy-qq.sh configure
bash deploy-qq.sh pair
bash deploy-qq.sh unbind
bash deploy-qq.sh uninstall
```

自定义运行目录时为每条命令添加 `--root DIR`。Windows 使用 `deploy-qq.ps1 -Action ACTION -Root DIR`。`configure` 更新凭据；`pair` 更换接收用户前要求终端输入 `REBIND`；`unbind` 立即取消待审批并清除绑定与截图。

凭据保存在专用运行目录 `qq/config.json`；本机接口访问凭据位于 `qq/local-token`。macOS/Linux QQ 目录权限 0700，敏感文件 0600；Windows 设置当前用户和 SYSTEM 的目录 ACL。运行状态只显示配置/在线/绑定状态和数值错误码，不显示凭据。审计日志 `qq/audit.jsonl` 记录随机编号及状态，不记录命令、截图或 QQ 消息正文。macOS 服务输出在 `logs/qq.err.log` / `logs/qq.out.log`；Windows 新守护只写 `logs/qq-supervisor.log` 生命周期记录，历史输出文件可保留但不再追加。

若鉴权出现网络错误，脚本会区分 `TLS_CERT_VERIFY_FAILED`、`DNS_LOOKUP_FAILED`、`CONNECTION_TIMEOUT`、`TLS_HANDSHAKE_FAILED` 和无效响应。macOS 的 python.org Python 可能没有安装 OpenSSL 默认 CA 文件；脚本会加载系统 `/etc/ssl/cert.pem`，保留证书和主机名校验，无需修改全局 Python。显式设置 `SSL_CERT_FILE` / `SSL_CERT_DIR` 时保留该覆盖。任何验证失败均保留已有凭据。

本机接口仅绑定 `127.0.0.1` 临时端口，并要求随机 token；拒绝携带浏览器 Origin 的请求。另占用 `local_port + 1000` 的回环端口防止同一运行目录启动多个 Bot。Windows/macOS 服务均在开发用户登录后运行；休眠、注销或网络断开期间不能保证实时推送。Linux 无人登录启动需要既有用户服务 linger 配置。

QQ 单独卸载停止服务，删除受管理的 QQ agent/skill、AppSecret、本机 token 和绑定；不修改 IDE/frp。完整家庭端卸载也先卸载 QQ。安装过的 skill 被手动修改后不会自动覆盖或删除。部署包使用白名单，不包含运行目录、凭据或日志。

## 验证边界与来源

本次定向自动化覆盖：接收者校验、重复事件、过期/取消/失联请求、一次性选项、发送失败回退、配对码、截图查找、回环接口认证、HTTP 200 业务错误、token 刷新及分片上传序列，以及真实子进程的 ACP 透传。Mock QQ 响应不能代替真实 AppID/AppSecret、账号权限、手机收发和模型操作验收。

2026-10-22 已在当前 mac1 部署用户 LaunchAgent、ACP 代理及用户截图 skill；真实 Codex adapter 通过代理响应 `initialize`，协议版本 1、认证方法 2 项。部署时尚未输入 QQ 凭据，不能据此宣称 QQ 真实上线、手机审批或截图收发已通过。当时 Windows/Linux 生命周期未在目标系统实测。

同日修复了当前 Python 无默认 CA 导致的鉴权失败：修复前实测 `SSLCertVerificationError`（verify code 20），系统 curl TLS 校验成功；修复后 Python 使用系统 CA，向官方接口发送虚构凭据并收到 QQ 业务错误 100007，证明 TLS/JSON 链路可用，不代表真实 AppID/AppSecret 验证通过。此轮仅运行 13 项 Python QQ 定向测试，全部通过，未重新执行全量回归。

随后用户完成真实 QQ 绑定，本机实测 `configured=true, online=true, bound=true`，已收到真实单聊指令。首次真实图片测试发现服务端从 1 开始返回分片序号，修复后已完整通过预上传、分片上传/确认、合并和富媒体发送，QQ 接口返回成功。测试图片为生成的 `QQ TEST` 标识卡，不包含项目或桌面数据；接口成功与手机实际显示分别验收。

真实 QQ + 已部署代理 + 模拟 ACP adapter 的审批通知已发送，5 分钟内未收到审批选择，代理收到 `cancelled` 并正常结束；未执行 shell、修改项目或调用模型。这验证了通知发送和超时取消路径，不能视为人工回复审批或真实 Codex 模型任务验收通过。上传修复最后运行 18 项 Node QQ 定向测试，全部通过；更新部署后再次确认 Bot 在线且绑定保留、无待审批请求。

随后重发的真实 QQ 审批往返已通过：用户收到并回复，代理对原 JSON-RPC 审批请求返回 `selected / smoke-allow-once`，模拟 adapter 返回 `QQ_ACP_ROUNDTRIP` 测试标记，本机待审批数为 0。该测试不执行 shell、不修改项目、不调用模型；真实 QQ 人工回复与代理回填已验证，真实 Codex 模型工具执行仍未验收。同时修正代理结束清理时覆盖已决定结果的审计问题：仅未决定请求可以被普通连接取消，已选定结果保留。

2026-10-23 在 Windows 完成 QQ 组件部署、真实凭据校验与网关上线，修复默认编码和 PowerShell 将原生 stderr 当终止错误的问题。清理已核实的旧 Bot 残留进程后状态为 `configured=true, online=true`；本轮没有把手机绑定、审批或截图往返记为已验收。25 项 Python 定向测试和 34 项 Node QQ 测试通过，详情见 [Windows 验证记录](evidence/windows-deployment.md)。

2026-10-24 在当前 Windows 部署无窗口 QQ 守护，实机受控终止 Bot 后约 5.0 秒恢复、终止守护后约 10.2 秒由计划任务恢复。主动 stop/start、无重复 Bot、凭据/绑定/token 哈希一致、IDE PID 不变均通过；最终状态为 `configured=true, online=true, bound=true`。23 项 Windows/QQ 定向测试与四个 PowerShell 入口解析通过。此轮不发送真实 QQ 消息，未重新验收手机审批/截图往返；详见 [Windows 验证记录](evidence/windows-deployment.md)。

- [QQ 鉴权](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [QQ API 地址与错误码](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html)
- [QQ WebSocket](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)
- [QQ 主动消息与频率限制](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [QQ 单聊上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html)
- [ACP 权限请求](https://agentclientprotocol.com/protocol/v1/tool-calls#requesting-permission)
- [OpenAI skill 工作流边界](https://developers.openai.com/plugins/concepts/skills)
