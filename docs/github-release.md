# GitHub 自动测试与 Windows EXE 发布

日常开发仍使用根目录的 `Transport_App.bat` 和 `.venv`。提交到 GitHub 后，
[Windows tests and EXE](https://github.com/ylylyl98/Transport_Pyside6/actions/workflows/windows.yml)
在独立的 Windows 2022 运行器上完成软件验证和打包，不使用实验室电脑或仪器。

## 触发与下载

| 操作 | 自动执行 |
| --- | --- |
| 推送 `main` 或向 `main` 提交 PR | 全部离线测试 → 构建 EXE → 验证 EXE → 保存可下载产物 |
| Actions 页面选择 Run workflow | 对选定分支执行相同流程 |
| 推送 `v*` 版本标签 | 相同验证通过后，创建 GitHub Release 并上传 ZIP 与 SHA256 校验文件 |

普通提交的 ZIP 在对应 Actions 运行页面的 **Artifacts** 中，保留 30 天。
正式版本在 [Releases](https://github.com/ylylyl98/Transport_Pyside6/releases) 下载。
测试日志、模块清单和打包自检证据保留 14 天，失败时也上传。
失败的测试或 EXE 自检会阻止发布；不会使用 `continue-on-error` 绕过失败。

解压整个 `Transport-windows-x64.zip`，打开 `Transport/Transport.exe`。
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
`build-info.json` 记录源码提交、Python 和依赖版本；`SHA256SUMS.txt` 校验 ZIP。

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
```

`dist/`、`build/`、`tmp/` 为生成物目录，不提交 Git。
