# 开发与维护工具

日常做测量请双击项目根目录的 [`Transport_App.bat`](../Transport_App.bat) 或 `Transport_App - Shortcut.lnk`。

| 工具 | 用途 |
| --- | --- |
| [Transport_Preview.bat](Transport_Preview.bat) | 启动离线界面预览，使用模拟图形与隔离设置；不能用于测量。依赖已配置好的根目录 `.venv`。 |
| [Update-TransportShortcut.ps1](Update-TransportShortcut.ps1) | 修复根目录快捷方式的目标、工作目录和图标。正式启动 BAT 也会调用它。 |
| [verify_quick_release.py](verify_quick_release.py) | 自动发现全部离线测试，支持不同机器分片、逐模块隔离和 CI 报告。 |
| [build_windows.py](build_windows.py) | 使用固定的 [Transport.spec](Transport.spec) 构建便携 EXE、版本信息和校验文件。 |
| [verify_frozen.py](verify_frozen.py) | 搬迁完整 EXE 包，在隔离配置下验证界面、历史进程和实际 PNG 导出。 |
| [build_installer.py](build_installer.py) / [Transport.iss](Transport.iss) | 使用 Inno Setup 将已验证的便携包构建为当前用户安装包，更新两种下载的校验文件。 |
| [Install-InnoSetup.ps1](Install-InnoSetup.ps1) | 下载固定 Inno Setup 7.1.1 x64，核验 SHA256 和 Pyrsys B.V. 的有效 Authenticode 签名，安装到本地生成物目录。 |
| [verify_installer.py](verify_installer.py) | 静默安装至隔离临时目录，验证安装版并卸载，检查用户设置与数据保留。 |

只修复快捷方式、不启动应用时，在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\Update-TransportShortcut.ps1
```

`-ExecutionPolicy RemoteSigned` 只对本次 PowerShell 进程生效，不更改系统的持久执行策略。

预览和样式重载的操作见 [减少开发时的重启](../docs/development-feedback.md)。程序源代码仍在原来的根目录及 `app/`、`controllers/` 等目录中，没有搬到 `tools/`。

GitHub 云端测试、下载 EXE 与版本发布见 [自动测试与发布](../docs/github-release.md)。

安装包构建使用固定的 Inno Setup 7.1.1 x64 编译器（2026-10-28 发布）；
它不是应用运行依赖，不随 pip 安装。
在仓库根目录、独立构建环境中先构建并验证便携包，再运行：

```powershell
& .\tools\Install-InnoSetup.ps1
python -B tools/build_installer.py --bundle dist/Transport --output dist
python -B tools/verify_installer.py --installer dist/Transport-Setup-windows-x64.exe --output tmp/installer-smoke
```

准备脚本将下载缓存到 `tmp/inno-download`，核验固定 SHA256 和有效的厂商签名，
再以当前用户身份将编译器安装到 `tmp/inno-setup-7.1.1`，无需管理员权限。
构建脚本默认使用该处的 `ISCC.exe`，也可用 `--iscc` 指定其他已核验路径。
编译器下载有厂商签名不表示生成的 Transport 安装包有签名；当前产物仍未签名。
构建的安装包为
`dist/Transport-Setup-windows-x64.exe`；`SHA256SUMS.txt` 同时列出安装包和便携 ZIP。
安装包验证会运行真实的安装与卸载程序，但使用临时目录、隔离设置与模拟硬件；
不能代替真实仪器验收。安装包参数、编译器版本和 CI 说明见发布文档。
