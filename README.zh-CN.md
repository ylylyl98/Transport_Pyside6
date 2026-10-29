# Transport Measurement

[English](README.md) | **简体中文**

用于电输运和光电流测量的 Windows 桌面软件，集成仪器控制、实时绘图、CSV
数据记录和独立历史分析。默认界面使用 PySide6 / Qt Quick。

[![Windows tests and EXE](https://github.com/ylylyl98/Transport_Pyside6/actions/workflows/windows.yml/badge.svg?branch=main&event=push)](https://github.com/ylylyl98/Transport_Pyside6/actions/workflows/windows.yml)
[![Latest release](https://img.shields.io/github/v/release/ylylyl98/Transport_Pyside6)](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)

**[下载 Windows x64 版](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)**
· [安装与配置](docs/user-setup.zh-CN.md)
· [文档索引](docs/README.md)
· [反馈问题](https://github.com/ylylyl98/Transport_Pyside6/issues)

![Qt Quick 测量工作区，显示模拟 Gate Scan 数据](docs/images/measurement-preview.png)

*真实程序界面；截图来自禁止硬件访问的离线预览，数据为模拟数据。*

## 快速启动：Windows 安装包

1. 打开[最新 Release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)，
   在 Assets 中下载 **`Transport-Setup-windows-x64.exe`**。
2. 为当前 Windows 用户运行安装程序，无需管理员权限。从开始菜单打开
   **Transport Measurement**（桌面快捷方式可选）。
3. 按所用仪器安装驱动，连接硬件前完成[首次配置](docs/user-setup.zh-CN.md#3-配置首次使用会话)。

默认安装目录为 `%LOCALAPPDATA%\Programs\Transport Measurement`。
仍提供便携版 **`Transport-windows-x64.zip`**：完整解压后打开
`Transport/Transport.exe`，保留 `TransportWorker.exe`、`_internal` 和所有随包文件。
`SHA256SUMS.txt` 包含两种下载的校验值。校验、更新和卸载见[配置指南](docs/user-setup.zh-CN.md)。

Transport 打开时会在后台检查 GitHub 正式发布，新版本可用时在 Qt Quick 顶部或 Widgets 状态栏显示提醒。
Qt Quick 中通过 **View → Check for updates** 手动检查；可关闭
**View → Check for updates automatically**。Widgets 中对应的 Tools 菜单项为
**Check for updates...** 和 **Automatically check for updates**。
更新需等待测量与清理完成后手动下载、安装，不会自动安装或重启。
尚无此功能的旧版本需先手动更新一次。关闭程序后停止检查，没有系统推送通知，详见配置指南。

EXE 已包含 Python 和程序所需的 Python 包，使用发布版**无需另装 Python、Git、
PySide6、NumPy 或 Matplotlib**。仪器系统驱动和可选厂商 SDK 仍需单独安装。

## 需要自行下载安装什么

按实际用途安装下列组件，并选择兼容你的 Windows 版本、设备型号和 x64 程序的驱动。

| 使用场景 | 需要自行安装 |
| --- | --- |
| 打开 EXE 或查看历史测量 | 安装包或完整便携 ZIP；不需要单独配置 Python 环境。 |
| 使用 NI DAQ 采集或模拟输出 | 对应设备的 [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html) 驱动/运行时。 |
| 通过 VISA 连接 Keithley、SRS Lock-in、SP-2300、APS100 或 LS335 | 兼容的 VISA 实现，例如 [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)。 |
| 使用 NI GPIB 转接器/控制器 | [NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html)；其他品牌使用其厂商支持的驱动。 |
| 使用 USB 转串口 | 转接器厂商要求的 Windows 驱动。 |
| 使用 attoDRY2100 | 厂商 Python SDK，并配置实际目录和设备地址。SDK 版本及其与打包程序的兼容性需在你的系统上验证，见[配置指南](docs/user-setup.zh-CN.md)。 |
| 从源码运行或开发 | [Python](https://www.python.org/downloads/windows/) x64；项目使用 **3.13.9** 验证。克隆和同步仓库需要 [Git](https://git-scm.com/downloads/win)。启动器负责安装 Python 依赖。 |

**包内的 `pyvisa`、`nidaqmx` 是 Python 接口包，不等于 NI-VISA、NI-DAQmx 系统驱动。**
下载地址、安装检查、配置示例和故障排查见[用户配置指南](docs/user-setup.zh-CN.md)。

## 测量与分析功能

下列名称与程序中的英文按钮、菜单保持一致，方便查找。

| 测量模式 | 用途 |
| --- | --- |
| **Vds Sweep** | 固定栅极偏压并扫描源漏偏压；可选 Keithley 或 NI DAQ AO 作为 Vds 源。 |
| **Gate Scan** | 扫描原始栅压或派生 Doping / E-field 轨迹，可增加反向扫描。 |
| **2D Map** | 选择快慢轴，在原始或派生坐标下采集一维轨迹或二维网格。 |
| **Photocurrent** | 按启用的栅压条件扫描 SP-2300 波长。 |
| **B-field Gate Scan** | 在一系列 APS100 磁场目标点重复执行栅压扫描。 |
| **B-field Sweep** | 按固定偏压条件扫描 APS100 磁场端点，支持往返。 |

实时绘图支持通道选择和四通道对比。**History** 打开独立历史窗口，支持曲线比较、
二维图、实测切线和归一化。分析结果可导出 PNG 和配套 CSV，不修改原始测量文件。
详细操作见[测量指南（英文）](docs/measurement-guide.md)和
[历史分析与导出指南（英文）](docs/history-and-export.md)。

![独立 Qt Quick 历史窗口，比较模拟测量曲线](docs/images/history-preview.png)

*历史窗口使用合成文件预览，不包含实验数据。*

## 首次测量与安全

> **实验室软件：连接仪器可能改变输出。** 连接前核对接线、设备地址、源模式、
> 电压限值和电流合规值。软件检查不能替代实验室规程和硬件联锁。

1. 打开 **Connections**，扫描/选择实际设备地址，核对源模式与保护设置。
   清空未使用设备的地址后点击 **Connect all**，再确认所需仪器的状态和读回值。
2. 打开 **Sample & files**，设置操作者、样品编号和可写的数据目录；
   按接线检查信号换算和通道映射。
3. 选择测量模式，设置轨迹、时间、平均次数和输出名称，检查计划轨迹后开始测量。
4. 观察实时图和状态。请求停止后，等待仪器操作和清理完成。
   **STOP / 电压归零不代表磁场已经归零。**
5. 检查保存的 CSV，通过 **History** 比较或导出结果。

数据写入 `<数据目录>/<操作者>/<样品编号>/`，具体子目录和元数据随测量类型而定。
请选择自己的保存位置；源码默认值是实验室示例。更新版本时保留数据和用户设置。
完整[安全行为说明（英文）](docs/measurement-guide.md#safety-behavior)包括
Keithley 与 DAQ 不同的断开、归零行为。

## 从源码运行

先安装兼容的 Python x64，并使其可从 `PATH` 调用。项目接受 **Python 3.12–3.14**，
CI 验证基线为 **3.13.9**。在 PowerShell 中执行：

```powershell
git clone https://github.com/ylylyl98/Transport_Pyside6.git
cd Transport_Pyside6
.\Transport_App.bat
```

根目录启动器创建或复用 `.venv`，按需安装固定版本的
[requirements.txt](requirements.txt)，并在相关内容变化后执行隔离启动检查。
开发使用正式根目录；工具和备份目录不作为替代启动位置。
详见[配置指南](docs/user-setup.zh-CN.md)和[开发说明（英文）](docs/developer-notes.md)。

需要旧 Widgets 界面时，在空闲状态正常关闭程序。在 PowerShell 中，进入安装目录
（通常为 `$env:LOCALAPPDATA\Programs\Transport Measurement`）或解压后的
`Transport` 文件夹运行 `.\Transport.exe --widgets`；源码用户在仓库根目录运行
`.\Transport_App.bat --widgets`。

## 测试、发布与帮助

GitHub Actions 在独立 Windows 运行器上执行全部离线测试，构建并验证 EXE。
推送 `main` 和提交 PR 会生成可下载的 Actions 产物；`v*` 标签通过验证后发布
安装包、便携 ZIP 和校验文件。每台运行器内逐个执行 GUI 测试模块，不同运行器之间并行。
这些软件检查使用模拟硬件，**不能认证真实仪器的行为**。

| 文档 | 内容 |
| --- | --- |
| [用户配置](docs/user-setup.zh-CN.md) / [English](docs/user-setup.md) | 下载、驱动、配置、更新与故障排查。 |
| [测量指南（英文）](docs/measurement-guide.md) | 扫描参数、实时图、数据输出与安全行为。 |
| [历史分析与导出（英文）](docs/history-and-export.md) | 曲线比较、二维图/切线、归一化、PNG 和 CSV 导出。 |
| [自动测试与发布](docs/github-release.md) | CI、构建产物、校验文件与版本标签发布。 |
| [开发说明（英文）](docs/developer-notes.md) | 源码布局、启动行为与实现细节。 |
| [文档索引](docs/README.md) | 仪器指南、迁移说明和带日期的审计记录，标明文档语言。 |

遇到问题时，可[提交 Issue](https://github.com/ylylyl98/Transport_Pyside6/issues)，
说明发布版本、Windows 版本、启动方式、相关仪器及驱动版本、报错原文和复现步骤。
分享日志或截图前去除个人路径和敏感测量数据。

## 许可证

仓库目前没有项目许可证文件。在项目所有者添加许可证前，不应假定已获准再分发或复用代码。
