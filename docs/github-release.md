# GitHub 自动测试与 Windows EXE 发布

日常开发仍使用根目录的 `Transport_App.bat` 和 `.venv`。提交到 GitHub 后，
[Windows tests and EXE](https://github.com/ylylyl98/Transport_Pyside6/actions/workflows/windows.yml)
在独立的 Windows 2022 运行器上完成软件验证和打包，不使用实验室电脑或仪器。

首次安装发布版的用户请先看[用户安装与配置](user-setup.zh-CN.md) / [User setup](user-setup.md)，
其中列出需要自行下载安装的驱动、SDK、首次配置和设置备份位置。

## 触发与下载

| 操作 | 自动执行 |
| --- | --- |
| 推送 `main` 或向 `main` 提交 PR | 全部离线测试 → 构建并验证便携 EXE → 构建并验证安装包 → 保存可下载产物 |
| Actions 页面选择 Run workflow | 对选定分支执行相同流程 |
| 推送 `v*` 版本标签 | 相同验证通过后，创建 GitHub Release 并上传安装包、便携 ZIP 与 SHA256 校验文件 |

普通提交的发布文件在对应 Actions 运行页面的 **Artifacts** 中，保留 30 天。
正式版本在[最新 Release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest) 下载：
`Transport-Setup-windows-x64.exe`（推荐）、`Transport-windows-x64.zip`（便携版），
以及同时校验两者的 `SHA256SUMS.txt`。
测试日志、模块清单和打包自检证据保留 14 天，失败时也上传。
失败的测试、EXE 自检或安装包验证会阻止发布；不会使用 `continue-on-error` 绕过失败。

安装包为当前 Windows 用户安装，无需管理员权限，默认目录为
`%LOCALAPPDATA%\Programs\Transport Measurement`；提供开始菜单快捷方式和可选桌面快捷方式。
安装器不安装硬件驱动。卸载使用 Windows“设置 → 应用 → 已安装的应用”，
保留 AppData 设置和测量数据。更新或卸载前先等待测量和仪器清理完成，
正常关闭 Transport、历史窗口和后台工作进程；数据保存在程序目录之外。
当前发布未签名；遇到未知发布者或 SmartScreen 提示应核对来源、版本和校验值，
无法确认时停止并联系维护者，不关闭 Windows 安全检查。

便携版解压整个 `Transport-windows-x64.zip`，打开 `Transport/Transport.exe`。
保留同目录下的 `TransportWorker.exe` 和 `_internal`；不能只复制单个 EXE。
目标电脑无需 Python。Widgets 回退入口为 `Transport.exe --widgets`。
替换版本前正常关闭程序，保留用户设置和测量数据。

## 测试覆盖与并行

`tools/verify_quick_release.py` 自动发现 `tests/` 和 `spectra_app/` 下的
`test_*.py`，包括原先未被总运行器覆盖的适配器、调试与控制器测试。
四个 GitHub Windows 运行器各执行一个确定性分片。每台机器使用 `--jobs 1`，
每个模块单独调用 `verify_offline.py`，隔离硬件访问、网络和用户配置。
分片间可以并行；同一台机器不并发运行 GUI/渲染测试，避免竞争导致误报超时。

模块不仅要退出成功，还必须产生本次运行的非空隔离验证报告。
报告缺失、全模块跳过或退出失败都会使该分片失败。`selection.json` 记录完整
模块清单和本分片模块，`summary.json` 与 Actions Summary 记录计数与耗时。
云端机器的字体、性能和渲染环境与实验室电脑有差异；云端耗时不是实机性能指标。

## 构建和 EXE 自检

Python 固定为 3.13.9；运行依赖使用 `requirements.txt`，构建工具使用
`requirements-build.txt`。Actions 缓存 pip 下载，依赖变化时自动换缓存。
构建脚本为 `tools/build_windows.py`，PyInstaller 配置为 `tools/Transport.spec`。
构建进程只使用 Python 与 Windows 系统搜索路径，并校验二进制来源，避免本机
其他软件的同名 DLL 混入包；PyInstaller 缓存也独立于其他项目。
QML、颜色表、样式和图标随包分发。GUI 与子进程共享依赖，避免重复打包。
历史查看器使用带标准输入输出的 worker，PNG 导出继续使用独立后台进程。

`tools/verify_frozen.py` 将完整产物复制到有空格的临时目录，从无关工作目录
启动真正的 EXE，并清除外部 Python/Qt 路径。它验证 Quick 界面、Widgets 回退、
历史窗口的独立进程通信，以及实际生成的四通道 PNG。此检查使用模拟数据，
安装硬件保护并隔离配置；不加载实验室用户设置，不连接仪器。
`build-info.json` 记录源码提交、Python 和依赖版本；`SHA256SUMS.txt` 校验安装包与便携 ZIP。

`tools/build_installer.py` 使用 Inno Setup 和 `tools/Transport.iss` 包装已验证的
`dist/Transport`，两种下载包含同一套应用负载。编译器固定为 Inno Setup 7.1.1 x64
（2026-10-28 发布），不属于 Python 依赖。`tools/Install-InnoSetup.ps1` 将官方下载
缓存到 `tmp/inno-download`，核验固定 SHA256 和 Pyrsys B.V. 的有效 Authenticode
签名，再以当前用户身份安装到 `tmp/inno-setup-7.1.1`。本机与 CI 均无需管理员权限。
构建脚本默认使用该目录中的编译器；厂商编译器有签名不表示 Transport 安装包已签名，
当前生成的 Setup 仍未签名。默认从 `build-info.json`
读取标签/引用确定版本；非标签构建使用开发版本，也可显式指定 `--version`。
`tools/verify_installer.py` 在隔离临时目录中执行真实安装、安装版验证与卸载，
并检查用户设置和数据保留。安装包附带同名 `.manifest.json` 验证清单；验证脚本
默认读取该清单。此清单是构建/自检证据，不需要用户额外下载才能运行安装器。

NI-DAQmx、VISA 驱动和可选厂商 SDK 仍需在目标电脑安装/配置。
真实电压输出、保护、停止/归零、磁体及温控行为仍需在受控实验室环境验收。
这些不能由 GitHub 托管运行器上的模拟测试替代。

## 发布一个版本

先审查并提交任务相关源码，确认目标 `main` 提交。然后创建一个未使用的版本标签：

```powershell
git tag -a vYYYY.MM.DD -m "Transport release vYYYY.MM.DD"
git push origin vYYYY.MM.DD
```

将示例日期替换成实际版本，也可采用语义化版本号。不要复用或移动已发布标签。
标签流水线验证同一提交，并在发布前复核标签没有移动且提交属于 `main` 历史；
普通 `main` 推送只保留 Actions 产物。
发布步骤只有标签任务具备 `contents: write`，不需要在仓库添加个人访问令牌。
第三方 Actions 固定到完整提交 SHA。已存在的 Release 不会被自动覆盖；
需要修复已发布版本时创建新标签。

发布新的正式 Release 后，包含更新检查模块的 Transport 会在打开期间从 GitHub
正式 `latest` 获取新版信息：启动约 10 秒后首次检查，之后每 6 小时检查一次。
Qt Quick 的 **View → Check for updates** 可手动检查，
**View → Check for updates automatically** 可关闭自动检查；Widgets 中对应入口为
**Tools → Check for updates...** 和 **Tools → Automatically check for updates**。
新版本通过 Qt Quick 顶部提示条或 Widgets 状态栏
提供官方下载入口，**Dismiss** 在当前会话隐藏；测量中不弹窗。
程序关闭时无后台检查或系统推送，也不会自动下载、安装或重启。用户需等待测量和
仪器清理完成、正常关闭程序后手动更新，设置和数据保留。尚无该模块的旧版本必须
先手动更新一次；推送源码或 Actions 产物本身不会触发正式新版提醒。

## 本机排查

仅在排查云端失败时才需要在本机重跑相应模块；日常完整验证交给 GitHub。

```powershell
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_process_command
.\.venv\Scripts\python.exe -B tools/verify_quick_release.py --list
.\.venv\Scripts\python.exe -B tools/verify_quick_release.py --jobs 1 --shard-count 4 --shard-index 0
```

本机打包建议使用独立的构建虚拟环境，安装两份 requirements 后运行：

```powershell
python -B tools/build_windows.py
python -B tools/verify_frozen.py --bundle dist/Transport --output tmp/frozen-smoke
& .\tools\Install-InnoSetup.ps1
python -B tools/build_installer.py --bundle dist/Transport --output dist
python -B tools/verify_installer.py --installer dist/Transport-Setup-windows-x64.exe --output tmp/installer-smoke
```

如使用其他已经核验的编译器位置，可通过 `--iscc` 传入实际 `ISCC.exe` 路径。
`dist/`、`build/`、`tmp/` 为生成物目录，不提交 Git。
