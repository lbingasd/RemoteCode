# 发布内容检查记录

日期：2026-10-23。对象：RemoteCode 当前仓库与部署工具包。

## 检查范围

- 当前 Git 跟踪文件，以及本地全部引用可达的 22 个既有提交中的文件对象；不包括远端未拉取分支、已删除且不可达的 Git 对象或外部发布附件。
- `RemoteCode-IP-deployment.zip`，以及其中/随包 NSSM ZIP、ACP Client VSIX 的嵌套文件；扫描私钥头、常见服务密钥格式、带凭据 URL 和敏感字段的字面量，并人工核实命中项。
- 两张 `docs/evidence/` PNG 的人工查看，防止仅靠文本扫描遗漏屏幕上的密码或 token。
- `.gitignore`、部署包白名单，以及本机机器配对目录是否被 Git 忽略。检查配对文件的发布状态，不读取或输出本机凭据内容。

## 结果

未发现真实私钥、QQ AppSecret、模型 API Key、真实访问 token 或含实际账号密码的 URL。命中项均为以下可发布内容：

- `scripts/cloud.py` 生成配置时的 `{machine['frp_token']}` 模板插值，并非固定凭据。
- 测试中的 `TEST_SECRET`、`KEEP_SECRET`、虚构 API key、`secret phrase`、`unchanged` 和用于验证镜像拒绝逻辑的 `user:SECRET` URL。
- 示例/文档中的密钥文件名、公开校验哈希及占位地址。公开哈希不等于认证凭据。

本机配对目录与归档未被 Git 跟踪，也未进入部署 ZIP。已在原有忽略规则上补充 enrollment 目录和 P12/PFX/PPK、常见 SSH 私钥文件名；部署包仍按白名单构建，新增收录根目录 `LICENSE`。

截图没有凭据。`browser-terminal-macos.png` 的终端提示符保留了测试机的用户名与主机名，README 示例也包含本机目录名；它们是可识别的本机信息，不是访问凭据。此轮保留原始验收证据，没有将其误称为完全匿名的材料。

本记录描述本次定向检查结果，不是对未来提交或任意编码/二进制内嵌秘密的完整检测保证。后续新增文件和发布包仍需在发布前检查。

## 许可

仓库新增 MIT `LICENSE`，仅覆盖 RemoteCode 自有代码与文档。第三方归档内的原许可和声明保留，入口为 `assets/README.md`；微软 VS Code Server 使用条款仍由安装者显式接受。
