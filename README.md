# ísmolar 同声传译

实时同声传译 + 字幕浮窗 + 中英对照文档的桌面工具。

**当前版本：v1.9.25 简洁界面版**（macOS DMG 为主版本，Windows 仅作同步测试版本）

## 功能

- 中英文混合识别，自动判断翻译方向
- 讯飞同传中译英 / 讯飞快译 + DeepSeek 精修英译中
- Azure AI Speech 实时语音翻译（欧洲节点，冰岛/海外使用推荐）
- 固定翻译（术语表）与参考稿件导入
- 稿件上传后自动后台预翻译，识别命中稿件句子时直接取用预译文
- 稳定字幕模式：区间内后台修正，结束时一次性追加，历史字幕不重写
- 停止后自动补译，输出中英对照 Word/TXT 文档
- 翻译记忆两级持久化：内存热缓存 + SQLite 磁盘记忆，跨会话复用
- macOS 本地一键打包 DMG

## 快速开始（macOS）

1. 双击 `①一键制作DMG.command` 本地打包，生成 `dist/ismolar-interpreter-macOS-local-vX.X.X.dmg`；
2. 或双击 `②先测试功能.command` 先跑源码测试；
3. 首次使用需在应用内填写讯飞 APPID / APIKey / APISecret 与 DeepSeek API Key。

## 冰岛/海外加速：Azure AI Speech（原型）

软件在国内节点（讯飞）之外新增了 Azure 实时语音翻译后端，适合冰岛等海外
场景：Azure 有欧洲区域（`westeurope` / `northeurope`），冰岛往返延迟远低于
直连国内节点，识别、翻译、译文语音一次流式返回，无需 DeepSeek。

使用步骤：

1. 到 Azure 门户创建 **Speech 服务**（免费层 F0 每月含 5 小时语音翻译），
   复制 Key 与区域（Region），建议选 `westeurope` 或 `northeurope`；
2. 打开软件 → **接口设置** → 服务提供商选择 **Azure AI Speech**，
   填入 Key 和区域，保存；
3. 翻译方向支持 中→英 / 英→中 / 自动识别；勾选“中英双向”后译文会实时朗读；
4. 术语表和翻译记忆照常生效；DeepSeek Key 在 Azure 模式下不需要填。

说明：

- 免费层 F0 每月 5 小时语音翻译，用于测试足够；正式高频使用需按量付费；
- “自动识别”模式当前只出字幕、不朗读译文；
- 打包 DMG 前需先确认 `azure-cognitiveservices-speech` 已装进构建环境
  （`requirements.txt` 已加入）。

## Windows（同步测试版）

推送 `main` 分支后，GitHub Actions 的 `build-windows` 工作流会自动构建 Windows 便携版 EXE：

1. Actions → `build-windows` → 手动运行或随推送触发；
2. 构建完成后在运行记录里下载 `ismolar-interpreter-win64-vX.X.X` 工件。

## 主要文件

| 文件 | 说明 |
|---|---|
| `app.py` | PySide6 主程序（界面、字幕浮窗、文档导出） |
| `xfyun_client.py` | 讯飞 WebSocket 客户端 + DeepSeek 翻译 + 翻译记忆 |
| `azure_client.py` | Azure AI Speech 实时语音翻译客户端（欧洲节点） |
| `ismolar_local.spec` / `ismolar_windows.spec` | PyInstaller 打包配置 |
| `scripts/build_macos.command` | macOS DMG 打包脚本 |
| `scripts/build_windows.bat` / `installer_windows.iss` | Windows 打包与安装包脚本 |
| `.github/workflows/build-windows.yml` | Windows 自动构建工作流 |
| `导入示例/` | 固定翻译与参考稿件格式示例 |

## 版本历史

- v1.9.13：接口设置中的 API 密钥改为明文显示，不再隐藏
- v1.9.12：网络断线自动重连（3 次），已识别内容不丢失
- v1.9.11：导出 Word 文档改为中文在上、英文在下的段落对照，不再使用表格
- v1.9.10：浮窗设置字号改为苹果风圆形步进器，颜色改为圆形色块+十六进制
- v1.9.9：字幕字体固定微软雅黑，新增字重选择（常规/中等/加粗）
- v1.9.8：字幕浮窗手动边缘/角落缩放，悬停显示双箭头光标
- v1.9.7：修复新机器首次运行翻译记忆磁盘层静默失效；数据库权限收紧
- v1.9.6：翻译记忆升级为内存热缓存 + SQLite 磁盘持久化
- v1.9.5：简洁界面版（苹果风 UI、讯飞快译 + DeepSeek 精修、稿件预翻译）
