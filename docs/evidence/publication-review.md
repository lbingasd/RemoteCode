# 发布内容检查记录

日期：2026-10-23。对象：RemoteCode 当前仓库与部署工具包。

## 检查范围

- 当前 Git 跟踪文件，以及本地全部引用可达的 22 个既有提交中的文件对象；不包括远端未拉取分支、已删除且不可达的 Git 对象或外部发布附件。
- `RemoteCode-IP-deployment.zip`，以及其中/随包 NSSM ZIP、ACP Client VSIX 的嵌套文件；扫描私钥头、常见服务密钥格式、带凭据 URL 和敏感字段的字面量，并人工核实命中项。
- 两张 `docs/evidence/` 截图的人工查看，防止仅靠文本扫描遗漏屏幕上的密码或 token。最初图片实际为 JPEG，展示时先修正了误用的文件后缀；随后按用户要求，对浏览器截图中的终端内容进行马赛克处理。
- `.gitignore`、部署包白名单，以及本机机器配对目录是否被 Git 忽略。检查配对文件的发布状态，不读取或输出本机凭据内容。

## 结果

未发现真实私钥、QQ AppSecret、模型 API Key、真实访问 token 或含实际账号密码的 URL。命中项均为以下可发布内容：

- `scripts/cloud.py` 生成配置时的 `{machine['frp_token']}` 模板插值，并非固定凭据。
- 测试中的 `TEST_SECRET`、`KEEP_SECRET`、虚构 API key、`secret phrase`、`unchanged` 和用于验证镜像拒绝逻辑的 `user:SECRET` URL。
- 示例/文档中的密钥文件名、公开校验哈希及占位地址。公开哈希不等于认证凭据。

本机配对目录与归档未被 Git 跟踪，也未进入部署 ZIP。已在原有忽略规则上补充 enrollment 目录和 P12/PFX/PPK、常见 SSH 私钥文件名；部署包仍按白名单构建，新增收录根目录 `LICENSE`。

原截图没有凭据，但浏览器截图的终端提示符包含测试机用户名、主机名与本机路径。当前展示版 `browser-terminal-macos-redacted.png` 已将终端文字区域整体加马赛克，布局测试截图未发现此类信息。马赛克由图片编辑工具处理，此图用作界面展示，具体验收结论以对应的 JSON/文字记录为准。部署 ZIP 与 README 同步引用处理后的图片。

README 中的目录名是公开部署示例，图片打码不等于仓库所有文字已匿名化。

## Git 历史清理

随后按用户要求使用 `git-filter-repo --sensitive-data-removal` 重写历史，移除以下路径在所有既有提交中的内容：

- `docs/evidence/browser-terminal-macos.png`
- `docs/evidence/browser-terminal-macos.jpg`
- `RemoteCode-IP-deployment.zip`
- `RemoteCode-IP-deployment.sha256`

远端检查只有 `main`，没有标签或 PR 引用。重写后重新构建并提交当前安全版部署包，包内仅收录打码后的浏览器截图和无敏感内容的布局测试图；旧原图文件名加入忽略规则。检查可达对象，确认原图和旧部署包的文件对象已不再被引用；同时清理本地保存旧包的 Codex 差异快照引用及 Git reflog/不可达对象。

更新远端采用带精确旧提交校验的 `--force-with-lease`，避免覆盖清理期间他人的新提交。其他电脑应重新 clone，避免将旧历史合并后推回。

强推只更新可控的 Git 引用，无法保证 GitHub 已缓存的旧提交页面、fork 或他人克隆同步消失。如需服务器端缓存清理，应按 [GitHub 官方流程](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository#fully-removing-the-data-from-github) 联系 GitHub Support；是否受理由 GitHub 按敏感数据标准判断。本次未提交 Support 工单，也未声称外部副本已被删除。

本记录描述本次定向检查结果，不是对未来提交或任意编码/二进制内嵌秘密的完整检测保证。后续新增文件和发布包仍需在发布前检查。

## 许可

仓库新增 MIT `LICENSE`，仅覆盖 RemoteCode 自有代码与文档。第三方归档内的原许可和声明保留，入口为 `assets/README.md`；微软 VS Code Server 使用条款仍由安装者显式接受。
