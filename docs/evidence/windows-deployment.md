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

## 未验收

Windows 重启/注销恢复、Headless 密码任务、完整卸载与升级生命周期、当前 Windows QQ 的手机绑定/审批/截图往返、真实模型工具任务及 iPad 浏览器操作。macOS 上已有的 QQ 验收不能替代这些 Windows 场景。
