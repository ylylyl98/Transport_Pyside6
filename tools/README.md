# 开发与维护工具

日常做测量请双击项目根目录的 [`Transport_App.bat`](../Transport_App.bat) 或 `Transport_App - Shortcut.lnk`。

| 工具 | 用途 |
| --- | --- |
| [Transport_Preview.bat](Transport_Preview.bat) | 启动离线界面预览，使用模拟图形与隔离设置；不能用于测量。依赖已配置好的根目录 `.venv`。 |
| [Update-TransportShortcut.ps1](Update-TransportShortcut.ps1) | 修复根目录快捷方式的目标、工作目录和图标。正式启动 BAT 也会调用它。 |
| [verify_quick_release.py](verify_quick_release.py) | 自动发现全部离线测试，支持不同机器分片、逐模块隔离和 CI 报告。 |
| [build_windows.py](build_windows.py) | 使用固定的 [Transport.spec](Transport.spec) 构建便携 EXE、版本信息和校验文件。 |
| [verify_frozen.py](verify_frozen.py) | 搬迁完整 EXE 包，在隔离配置下验证界面、历史进程和实际 PNG 导出。 |

只修复快捷方式、不启动应用时，在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\Update-TransportShortcut.ps1
```

`-ExecutionPolicy RemoteSigned` 只对本次 PowerShell 进程生效，不更改系统的持久执行策略。

预览和样式重载的操作见 [减少开发时的重启](../docs/development-feedback.md)。程序源代码仍在原来的根目录及 `app/`、`controllers/` 等目录中，没有搬到 `tools/`。

GitHub 云端测试、下载 EXE 与版本发布见 [自动测试与发布](../docs/github-release.md)。
