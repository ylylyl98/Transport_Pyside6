# 安装与首次配置

[English](user-setup.md) · [项目介绍](../README.zh-CN.md)

日常操作和历史数据分析使用 Windows EXE；需要开发或修改 Transport 时使用源码
启动器。两种方式默认都打开当前的 Qt Quick 界面。

## 1. 下载并安装 Windows 版

日常使用推荐打开[最新 Release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)，
在 Assets 下载 **`Transport-Setup-windows-x64.exe`** 和 `SHA256SUMS.txt`。
在 PowerShell 中检查安装包，将结果与校验文件中对应条目比较：

```powershell
Get-FileHash .\Transport-Setup-windows-x64.exe -Algorithm SHA256
```

为当前 Windows 用户运行安装程序，无需管理员权限。默认目录为
`%LOCALAPPDATA%\Programs\Transport Measurement`。安装器创建开始菜单快捷方式，
桌面快捷方式可选。安装后从开始菜单打开 **Transport Measurement**。
安装器不安装仪器驱动或厂商 SDK，请按下文单独准备。

当前发布文件未签名，Windows 可能显示未知发布者或 SmartScreen 提示。作出信任
决定前核对仓库、发布版本和校验值。如果提示异常或无法确认文件来源，请停止并联系
维护者。不要关闭 Windows 安全检查。

### 便携 ZIP 替代方式

1. 打开[最新 Release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)，
   选择版本，在 **Assets** 中下载 `Transport-windows-x64.zip` 和
   `SHA256SUMS.txt`。GitHub 自动生成的 **Source code** 压缩包不包含打包后的程序。
2. 可在 PowerShell 中检查下载的 ZIP：

   ```powershell
   Get-FileHash .\Transport-windows-x64.zip -Algorithm SHA256
   ```

   将结果与 `SHA256SUMS.txt` 中该 ZIP 对应的哈希值比较。
3. 在 Windows x64 电脑上，将 ZIP **完整解压**到本地文件夹，打开其中的
   `Transport/Transport.exe`。保留同目录的 `TransportWorker.exe`、`_internal`
   和其余随包文件；不要在压缩包内启动，也不要只复制主 EXE。
4. 查看已有测量数据时，使用主窗口的 **History**，或下文的独立历史查看器命令。
   查看历史数据不需要连接仪器。

EXE 已包含 Python 运行时和应用库，无需另装 Python、PySide6、Matplotlib，
也无需单独安装 `requirements.txt` 中的 Python 包。仪器驱动和可选的
attoDRY2100 SDK 仍需按下表单独准备。

### 更新与卸载

Transport 启动约 10 秒后在后台检查 GitHub 正式 **latest** 发布，运行期间每 6 小时
再次检查。在 Qt Quick 中，使用 **View → Check for updates** 手动检查，通过
**View → Check for updates automatically** 关闭或开启自动检查。Widgets 中对应入口为
**Tools → Check for updates...** 和 **Tools → Automatically check for updates**。发现新版本时，
Qt Quick 顶部提示条或 Widgets 状态栏提供下载入口；**Dismiss** 在当前会话隐藏提示。
检查不会在测量中弹窗。
仅在程序打开时检查；关闭 Transport 后停止检查，没有系统推送通知。尚无此模块的
旧版本需先手动更新一次，才能获得后续新版提醒。

通过提示条前往官方 Release 下载，按下文步骤在空闲时更新。程序不会自动下载、
安装或重启。

更新或卸载前，等待测量和仪器清理完成，正常关闭 Transport，包括历史窗口和后台
工作进程。仍有 Transport 进程运行时，不要替换程序文件。备份第 5 节所述设置和
测量数据。

安装版更新时运行新安装包，选择相同安装目录。卸载使用 Windows **设置 → 应用 →
已安装的应用 → Transport Measurement → 卸载**。卸载移除安装的程序文件和
快捷方式，保留 AppData 设置、测量数据和未知文件。更新覆盖现有程序包；新版不再
使用的旧程序文件保留到卸载时移除。便携版更新时解压到另一个文件夹，确认
新版可用前保留旧版完整目录；关闭程序后才能删除便携目录。
测量数据应保存在两种程序目录之外。同一 Windows 用户下，安装版、便携版和
源码版共用设置。`SHA256SUMS.txt` 同时包含安装包与便携 ZIP 的校验值。

## 2. 哪些软件需要自己安装

只安装实际硬件和用途需要的项目。在厂商下载页面选择支持具体设备型号和
Windows 版本的驱动。

| 用途 | 额外安装内容 | 在 Transport 中连接前的识别检查 |
| --- | --- | --- |
| 打开 EXE 或查看历史数据 | 无需下列仪器驱动；无需另装 Python | 打开程序或已有数据文件夹 |
| 使用 NI DAQ 采集 | [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html) | 在 NI MAX 中确认设备，记录 `Dev1` 等实际设备名 |
| 使用 VISA 仪器：Keithley、SRS 锁相放大器、串口单色仪、APS100 或 LS335 | 兼容的 VISA 实现，例如 [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) | 在 NI MAX 或 Transport 的 **Scan resources** 中列出对应 GPIB/ASRL 资源 |
| 使用 NI GPIB 接口 | [NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html)，并安装 VISA | 在 NI MAX 中确认 NI 接口和 GPIB 总线编号 |
| 使用 USB 转串口适配器或厂商 USB 接口 | 若 Windows 尚未安装，使用该适配器/仪器厂商提供的驱动 | 在 Windows 设备管理器中确认设备和 COM 编号，先解决驱动报错 |
| 通过 SDK 后端使用 attoDRY2100 | 从 [attocube](https://www.attocube.com/en/contact) 获取匹配的厂商 Python SDK 及其要求的组件 | 检查本机 SDK 目录和配置的主机地址，详见下文 |
| 运行或修改源码 | [Python 3.13.9](https://www.python.org/downloads/release/python-3139/) 的 **Windows installer (64-bit)**；[Git for Windows](https://git-scm.com/install/windows) 可选 | 确认 `python --version`；BAT 会创建本地环境并安装应用依赖 |
| 在本机构建 EXE | 源码依赖及 [requirements-build.txt](../requirements-build.txt) | 使用下文的独立构建环境 |

Python 包 `nidaqmx` 和 `pyvisa` 不能替代系统 DAQ/VISA 驱动。NI-488.2 针对
NI 的 GPIB 硬件，其他品牌的 GPIB 接口应使用对应厂商支持的驱动组合。Transport
常规流程不使用 LightField，也不会因为 Python 依赖中包含 `pythonnet` 就要求
额外安装 LightField/.NET。具体厂商 SDK 若有额外要求，应遵循其说明。

按各厂商要求的顺序安装；安装器要求重启时先重启。首次识别只查看设备和资源列表，
不要用输出测试面板、复位或修改配置的命令进行发现检查。
**Scan resources** 列举 GPIB、串口 VISA 和 NI DAQ 资源；空列表也可能表示缺少
驱动或 VISA 后端。**Connect all** 会进行真实设备初始化，可能改变源表设置，
使用前先完成下一节的检查。

## 3. 配置首次使用会话

点击顶部的 **Instruments**，在面板内的选择器中切换下列页面。同一 Windows
账户下曾使用过 EXE 或源码版时，程序可能已经载入之前保存的设置。

1. **Sample & files：**填写 **Operator**、**Device / sample** 和
   **Data folder**。用 **Browse…** 选择可写的本地目录，例如
   `C:\Measurements`。文件按 `Data folder / Operator / Device` 分层保存。
   操作者和样品名称会转换为适合文件夹的名称，建议使用字母、数字、`-`、`_`
   或 `.`。如果本机没有旧设置或示例中的 `D:` 盘，请更换目录。
2. **Connections & protection：**使用 **Scan resources**，再填写各仪器的
   实际 VISA 地址和 NI DAQ 设备名。`GPIB0::23::INSTR` 或 `ASRL6::INSTR`
   只是示例，并非通用默认值；仪器地址和 GPIB 接口编号都要核对。
   不使用的仪器应清空地址，不保留其他实验室的无关默认值。
3. 在 **Connect all** 前，依据样品和接线逐一核对源表的 **Mode**、
   **Maximum source voltage (V)** 和 **Current compliance (A)**。
   连接后检查状态及保护读回，需要应用配置时使用 **Apply G1/G2/G3 protection**。
   修改地址或源模式后重新连接。配置中填写的限制不等于仪器已经验证该限制。
4. 对照采集通道映射检查 DAQ 接线。普通采集使用 AI0–AI3；启用 Drag / Drive
   后增加 AI4，需要重新连接 DAQ。在测量表单中选择 **NI DAQ AO** 作为 Vds 源时，
   选择实际接到样品的 AO 通道并核对输出范围。输入映射并非任意选通道的界面，
   具体分配见 [Drag / Drive 接线说明](drag-drive.md)。
5. **Signal chain** 和 **Drag / primary lock-in：**检查前放灵敏度、锁相校准、
   参考设置及样品 AC 估算使用的电压比例。仅在已配置第二台锁相放大器及相应接线时，
   启用 **Use Drive lock-in for Drag / Drive**。详见
   [锁相控制](lockin-control.md) 与 [Drag / Drive](drag-drive.md)。
6. 使用磁体或温控器时，先完成下文的配置。在 **Magnet & temperature** 中选择
   正确系统，展开 **Review commissioning details**，核对实际装置后再勾选
   **I reviewed the configured limits and connections**。连接和控制前需要
   完成这项审核。
7. 开始测量前复核轨迹、范围、源选择、时序和保存位置。
   **View → Save channel PNGs after measurements** 控制自动导出图片；
   **History** 打开当前配置样品的历史数据。

云端测试使用模拟设备验证软件，不能确定新装置的安全限制或接线。正式测量前，
应在实验室受控环境中验收真实输出、保护、停止/归零、磁体和温控行为。

## 4. 磁体、温控与 attoDRY2100 配置

硬件连接和调试参数保存在下一节所列的共享 JSON 配置中。Quick 的
**Runtime settings** 页面不能编辑 SDK 路径或这些连接参数。编辑 JSON 前正常
关闭 Transport，保留无关键值，修改后重新启动以载入设置。

| 系统 | 需要审核的配置 |
| --- | --- |
| attoDRY1000 / APS100 | `magnet.visa_resource`，以及与已调试磁体一致的线圈常数、磁场/电流/速率限制和加热器时序 |
| Lake Shore 335 | `lakeshore335.visa_resource`、样品/储液温度通道分配、温度限制、加热器档位和热保护联锁设置 |
| attoDRY2100 / SDK | `attodry2100.sdk_directory`、`host`、`channel`，以及磁场和温度限制 |

APS100/LS335 的调试和样品温控操作还应阅读
[attoDRY1000 样品温控说明](guides/SAMPLE_TEMPERATURE_1000.md)。

attoDRY2100 的配置步骤：

1. 从 attocube 获取适合控制器/固件的 SDK。程序需要提供 `Device` 的 Python 包，
   请保留完整厂商 SDK，不能替换成无关的 attoDRY DLL 封装库。
2. 将 `attodry2100.sdk_directory` 设置为包含 `atto_device/__init__.py` 的目录，
   或 `atto_device` 包目录本身。内置示例
   `D:\Insturment control v3\CRYO2100` 是另一台电脑的路径，必须更换。
   例如，将以下路径合并进现有的顶层 `attodry2100` 对象：

   ```json
   {
     "attodry2100": {
       "sdk_directory": "C:\\InstrumentSDKs\\CRYO2100"
     }
   }
   ```

   这里只是路径示例，不是完整的实机调试配置。如果配置文件尚不存在，创建其父目录
   和合法的 JSON 对象，再补充实际装置需要的设置。设置真实的主机地址和通道，
   审核全部限制，不要照搬其他系统的限制值。
3. 重启 Transport，选择 **attoDRY2100 / SDK**，连接前核对显示的地址和限制。
   SDK 温控功能取决于具体 SDK 和设备提供的能力。

attoDRY2100 后端已有离线测试覆盖，但**尚未完成实机验收**。具体厂商 SDK 及其
额外依赖与冻结 EXE 的兼容性仍需验证。在系统 Python 中另装包不会将它们加入
EXE 自带的运行环境；如需额外打包支持，请报告 SDK 版本和完整的加载错误。

## 5. 设置、数据与备份

Windows 下的默认位置：

| 内容 | 位置及用途 |
| --- | --- |
| GUI 设置 | `%APPDATA%\MyLab\TransportUI.ini`：地址、源表保护配置、数据目录、信号链、布局和历史查看偏好 |
| 共享配置 | `%APPDATA%\SpectralSweep\config.json`：磁体/温控配置、SDK 路径和运行设置 |
| 测量与分析文件 | **Sample & files** 中选择的 **Data folder**，包含操作者/样品子目录 |

可将含 `%APPDATA%` 的路径粘贴到文件资源管理器中打开。设置按 Windows 用户保存，
EXE 与源码程序共用，不在每份下载的 EXE 旁各存一份。
`SPECTRALSWEEP_CONFIG_PATH` 可覆盖 JSON 文件路径，但不会改变 INI 文件位置。
用户目录 JSON 不存在时，程序可能读取旧的项目根目录 `config.json`；之后保存时
写入用户配置位置。

更新、换电脑或手动编辑前，正常关闭程序，备份已经存在的两份设置文件，以及
**完整测量目录**，包括 CSV、元数据、日志、图片和分析附属文件。恢复时先关闭
程序，再将备份放回对应位置。换电脑后，连接前重新核对地址、盘符、SDK 路径、
接线和保护限制。回退软件版本会保留这些设置，因此试用新版本时也应保留匹配的
设置备份。

## 6. 从源码启动、使用回退界面或独立打开历史

安装发布构建使用的标准 64 位
[Python 3.13.9 Windows installer](https://www.python.org/downloads/release/python-3139/)，
使 `PATH` 中可以找到 Python。启动器接受 Python 3.12–3.14；项目固定使用
3.13.9 进行验证和构建。可从仓库 **Code → Download ZIP** 下载并解压源码，
也可安装可选的 Git 后克隆：

```powershell
git clone https://github.com/ylylyl98/Transport_Pyside6.git
cd Transport_Pyside6
python --version
.\Transport_App.bat
```

使用源码 ZIP 时，打开解压后的项目根目录并双击 `Transport_App.bat`。
根目录 BAT 会创建 `.venv`，按需安装 `requirements.txt` 并检查启动环境；
首次运行需要访问 Python 包索引。请使用根目录启动器，不使用迁移副本或历史备份。

下文安装版命令先在 PowerShell 中执行
`Set-Location "$env:LOCALAPPDATA\Programs\Transport Measurement"`
（或进入自选安装目录）。便携版命令在解压后的 `Transport` 目录执行；
源码命令在仓库根目录执行。

正常关闭当前界面后，可使用保留的 Widgets 界面：

```powershell
# 在安装目录或便携版解压后的 Transport 目录中
.\Transport.exe --widgets

# 在源码项目根目录中
.\Transport_App.bat --widgets
```

独立 Qt Quick 历史查看器指向已有数据目录，无需连接仪器：

```powershell
# 在安装目录或便携版解压后的 Transport 目录中
.\Transport.exe --history --folder "C:\Measurements"

# BAT 准备好 .venv 后，在源码项目根目录中
.\.venv\Scripts\python.exe -B transport_UI.py --history --folder "C:\Measurements"
```

本机构建 EXE 是可选操作。在 Git 克隆目录中使用已验证的 Python，将构建工具
安装到独立环境：

```powershell
python -m venv tmp\build-venv
.\tmp\build-venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\tmp\build-venv\Scripts\python.exe -B tools\build_windows.py
.\tmp\build-venv\Scripts\python.exe -B tools\verify_frozen.py --bundle dist\Transport --output tmp\frozen-smoke
```

ZIP 输出到 `dist`。自动测试、构建验证和发布流程见
[GitHub 发布说明](github-release.md)，界面回退细节见
[Qt Quick 迁移说明](qt-quick-migration.md)。
如需同时构建安装包，先运行 `& .\tools\Install-InnoSetup.ps1`，为当前用户下载、
核验并安装固定的 Inno Setup 7.1.1 x64 编译器，再按发布文档运行
`tools/build_installer.py` 和 `tools/verify_installer.py`。
Inno Setup 仅用于打包，使用应用无需安装。

## 7. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| EXE 复制或解压后无法启动 | 将完整 Release 附件重新解压到新目录，保留两个 EXE 和 `_internal`；检查安全软件是否隔离了随包文件，并保留完整报错 |
| VISA 列表为空或提示缺少 VISA 后端 | 安装适用的 VISA/接口驱动，在厂商工具中确认资源，核对实际 GPIB 总线或 COM 编号；单独安装 `pyvisa` 不够 |
| 找不到 NI DAQ 或名称不同 | 在 NI MAX 中确认 NI-DAQmx 和设备名，更新 **NI DAQ device**；确认设备具有所需 AI/AO 通道和 AO 读回功能 |
| 无法保存或目录不存在 | 选择本机可写的 **Data folder**，检查盘符和剩余空间，再检查 Operator 与 Device / sample |
| 保护、源模式或就绪状态阻止 Start | 阅读具体原因，确认源表配置及读回，根据情况重新连接/应用已审核配置；不要为了消除报错而提高限制 |
| 找不到 SDK 目录或包 | 修正用户目录 JSON 中的路径，核对 `atto_device` 包结构后重启；报告依赖问题时保留 SDK 版本和完整错误 |
| 更新后仍然出现旧设置 | EXE 和源码共用用户设置；检查 INI/JSON 路径及 `SPECTRALSWEEP_CONFIG_PATH` 覆盖值，修改前先备份 |
| BAT 找不到 Python 或无法安装包 | 检查 `python --version`、64 位安装和 `PATH`，以及包索引/网络访问；从终端运行根目录 BAT 以保留报错 |

报告问题时提供 Release 标签或 `build-info.json`、Windows 版本、相关驱动/SDK
版本和完整错误。硬件问题还应提供仪器型号和接口类型；分享日志前移除私密数据。
