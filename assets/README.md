# 随包的小型依赖

一键入口优先使用此目录的资源，每次使用仍核对对应 examples lock 的 SHA-256，避免 NSSM/扩展市场暂时不可达时阻塞安装。完整 VS Code Web Server、frp、系统依赖、npm 包和 Docker 镜像不包含在工具包中，按部署文档下载或预热缓存。

| 资源 | 来源和验证 | 许可 |
|---|---|---|
| NSSM 2.24-101-g897c7ad ZIP | [上游](https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip)；与[微软 WinGet manifest](https://github.com/microsoft/winget-pkgs/blob/master/manifests/n/NSSM/NSSM/2.24-101-g897c7ad/NSSM.NSSM.installer.yaml)的 SHA-256 一致；另从 Jellyfin 官方仓库取得的归档也有相同哈希 | Public Domain；原始 README、源码和声明在 ZIP 内保留 |
| ACP Client 0.2.0 VSIX | [微软扩展市场](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client)固定版本下载；SHA-256 是本次官方 HTTPS 下载内容指纹，不声称是独立签名 | 原始许可与第三方声明在 VSIX 的 `extension/LICENSE.txt` 和 `extension/dist/extension.js.LICENSE.txt` 内保留 |

不要把个人配对包、CA 私钥、AI 凭据放进此目录。打包脚本只收录上述两个已核实资源，不会打包 downloads 或 state。
