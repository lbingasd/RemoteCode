# Windows 部署验证记录

日期：2026-10-23。环境：Windows x64，原生 Windows PowerShell 5.1，Python 3.14.5，Node.js 24.16.0。

## 已验证的修复

- `deploy-local.ps1`：省略 `-Cache` 时在参数绑定后初始化缓存路径，避免 `$PSScriptRoot` 为空导致安装入口失败。
- Windows ACL：对已有文件递归设置有效权限，再为运行目录根设置可继承权限；运行目录文件能够读取/执行，重复执行权限设置后仍可访问，新文件可创建。
- WebView：识别来自已校验 Windows 1.143.0 下载包的 workbench SHA-256，同时保留原构建哈希；拒绝未知或被改动的构建。
- `deploy-qq.ps1`：启用 Python UTF-8 模式，避免中文 skill/设置文本按 Windows 默认编码读取失败。
- QQ 计划任务 runner：允许正常原生 stderr 输出，以进程退出码报告结果，避免 PowerShell `Stop` 将 Node 启动日志当成终止错误。

## 定向自动化

在仓库根目录运行：

```powershell
$env:PYTHONUTF8 = '1'
python -m unittest tests.test_windows_deployment tests.test_webview_compat tests.test_qq_bot
node --test tests/test_qq_bot.mjs
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\check-powershell.ps1
```

结果：

| 检查 | 结果与边界 |
|---|---|
| Python 定向测试 | 25 项通过；包括 3 项原生 Windows 测试、7 项 WebView 测试、15 项 QQ 配置/代理测试 |
| 原生 Windows 测试 | PowerShell 5.1 无 Cache 参数启动；NTFS ACL 重复设置后的脚本执行；含空格路径下 QQ runner 接收 stdout/stderr 并保留退出码 0/7。计划任务注册被 mock，不改真实任务 |
| Node QQ 测试 | 34 项通过（包含子测试）；覆盖审批、绑定、上传、回环 API、网关和真实 stdio 代理往返，QQ 接口使用模拟响应 |
| PowerShell 检查 | 3 个部署脚本解析通过，5 项 CRT 参数转义断言通过；转义断言本身不代表所有 Windows 原生命令行场景均已覆盖 |

测试中的 Python 子进程在 Windows 默认使用 `python`，可用 `REMOTECODE_PYTHON` 指定解释器，避免误启动 WindowsApps 中的 `python3` 商店占位程序。首次受限测试遇到临时目录 rename 权限错误；允许本机临时目录操作后重新运行通过。未运行全部跨平台集成测试。

## 本次部署的实机证据

- 家庭运行目录完成安装，NSSM 管理的 frpc 服务和 IDE 计划任务启动。
- 本机 IDE HTTP 检查及云端 TLS 健康检查通过；TLS 协商为 1.3。
- QQ 组件、专用 ACP 条目和受管理的截图 skill 已部署；用户在交互终端输入凭据，QQ 官方验证成功。
- 修复 runner 并核实、清理旧 Bot 残留进程后，本机状态为 `configured=true`、`online=true`；最后一次部署检查的 `bound=false`，已进入交互配对流程，不将其记为手机绑定成功。
- 本机曾遇到 Windows Defender 隔离 frp；用户授权处理后继续安装。部署脚本不自动调整 Defender 配置，该机器上的处理不等于所有 Windows 环境均无防护拦截。

本记录不包含 AppSecret、token、配对码、QQ 用户标识、浏览器密码或机器配对包。

## 2026-10-24：IDE 无窗口守护与自动恢复

本次断开时 frpc 系统服务正常、云端 TLS 通过，本机 IDE 端口拒绝连接。IDE 计划任务上次退出码为 `3221225786`（`0xC000013A / STATUS_CONTROL_C_EXIT`），[微软定义为控制台终止](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55)；任务历史日志未启用，不能确认具体操作者或结束方式。没有观察到同一时段的系统重启。旧任务直接启动 Node，仅凭已有失败重试设置未能恢复本次停止。

修复后由 `pythonw.exe` 启动 `windows_ide.py`：IDE 使用 `CREATE_NO_WINDOW`，无论退出码 0 或非 0，均在约 5 秒后重启；Windows Job Object 在守护进程结束时清理 IDE 子进程树，文件锁防止重复守护实例。计划任务保留原有登录/开机触发，并增加无限期每分钟恢复触发，使用 `IgnoreNew` 防止重复启动；主动 Stop 会先禁用任务，Start 再启用。

升级保留现有任务用户及凭据，接受受管理的旧 Node 动作并迁移为新动作；Windows 返回短用户名时先补全本机域再解析 SID，仍检查与安装用户一致。隐藏的 UAC 助手统一写 UTF-8 诊断日志，状态查询继续直接显示结果。

定向自动化：`python -m unittest tests.test_windows_ide tests.test_windows_deployment tests.test_deployment.DeploymentTests.test_ide_uses_token_file_and_matching_base_path` 共 7 项通过。覆盖原生无窗口进程、退出码 7/0 后重启、重复启动保护、父进程终止后子孙进程清理、带空格路径、短用户名 SID 解析、每分钟无限期恢复配置及原有部署回归。PowerShell 三个入口解析与五项 CRT 转义检查通过。测试不注册真实计划任务。

随后在本机现有 IDE 上进行受控测试：

- 核实子进程可执行路径和父 PID 后结束 IDE，原守护进程约 **5.3 秒**内启动新 IDE。
- 核实守护进程路径和命令后结束守护，计划任务约 **50.8 秒**内启动新守护及 IDE。
- 两次测试后仅一个 IDE 服务进程；本机 HTTP、云端 TLS 1.3、云端认证入口 HTTP 200 及 workbench 页面标记均通过。

生命周期日志只保存时间、PID、重试和退出码；IDE stdout/stderr 不写入日志，避免保存带 token 的启动地址。测试会终止正在运行的进程，不证明终端命令、模型会话或未保存编辑可在故障后继续。未实测注销/重启/Headless 模式；默认任务仍要求开发用户处于登录状态。

## 2026-10-24：QQ 无窗口守护与自动恢复

本轮开始时 QQ 计划任务为 Ready，最后退出码同样为 `3221225786`，本机 QQ 接口不可用。已将旧 PowerShell runner 迁移为 `pythonw.exe` + `windows_qq.py`，共用 `windows_supervisor.py` 的无窗口、退出重启、进程树清理机制，QQ 使用独立的 `qq-supervisor` 锁/状态/生命周期日志。计划任务保留用户登录边界，新增每分钟无限期恢复触发；主动 stop 先禁用，start 再启用。

自动化：`python -m unittest tests.test_windows_qq tests.test_windows_ide tests.test_windows_deployment tests.test_qq_bot` 共 **23 项通过**。包括原生 QQ wrapper、IDE/QQ 的退出码 0/7 重启和子孙进程清理、无控制台与重复启动保护、带空格路径、旧任务迁移、主动停用/移除和拒绝外部动作/其他任务用户等。四个 PowerShell 入口解析及五项 CRT 转义检查通过。

任务模拟测试曾因 PowerShell 模块自动加载覆盖拦截函数，注册一个仅指向临时目录的测试任务。已按任务名、TEMP 路径、可执行文件和启动参数核实并移除；修正为先加载模块、显式全局拦截并逐条断言，随后全部测试通过。真实 IDE/QQ 任务未在该失败测试中被修改。

实机部署及受控恢复结果：

- 部署前后 `qq/config.json`、`qq/binding.json`、`qq/local-token` 的哈希一致；不输出这些文件内容。
- 经路径及父 PID 核实后结束 Bot 进程，约 **5.0 秒**恢复；结束 QQ 守护进程后，约 **10.2 秒**由任务恢复，只有一个 Bot 实例。该时间取决于每分钟触发相位，不代表固定恢复耗时。
- 真实 `stop` 后任务禁用且 Bot/守护进程退出；`start` 后重新上线。
- 最终 `configured=true, online=true, bound=true`。IDE 守护和 IDE 子进程 PID 与部署前一致，本轮未重启 IDE。

本轮不发送真实 QQ 消息，也不重复验收手机审批/截图往返。正常或异常重启不恢复待审批请求，默认任务仍需用户登录。

## 其余未验收场景

Windows 重启/注销恢复、Headless 密码任务、完整卸载与升级生命周期、当前 Windows QQ 的手机绑定/审批/截图往返、真实模型工具任务及 iPad 浏览器操作。macOS 上已有的 QQ 验收不能替代这些 Windows 场景。
