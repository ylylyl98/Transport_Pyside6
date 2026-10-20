# 开发与维护工具

日常做测量请双击项目根目录的 [`Transport_App.bat`](../Transport_App.bat) 或 `Transport_App - Shortcut.lnk`。

| 工具 | 用途 |
| --- | --- |
| [Transport_Preview.bat](Transport_Preview.bat) | 启动离线界面预览，使用模拟图形与隔离设置；不能用于测量。依赖已配置好的根目录 `.venv`。 |
| [Update-TransportShortcut.ps1](Update-TransportShortcut.ps1) | 修复根目录快捷方式的目标、工作目录和图标。正式启动 BAT 也会调用它。 |

只修复快捷方式、不启动应用时，在项目根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\Update-TransportShortcut.ps1
```

`-ExecutionPolicy RemoteSigned` 只对本次 PowerShell 进程生效，不更改系统的持久执行策略。

预览和样式重载的操作见 [减少开发时的重启](../docs/development-feedback.md)。程序源代码仍在原来的根目录及 `app/`、`controllers/` 等目录中，没有搬到 `tools/`。
