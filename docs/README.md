# 文档索引

日常测量请运行项目根目录的 [`Transport_App.bat`](../Transport_App.bat)，或根目录快捷方式 `Transport_App - Shortcut.lnk`。简明说明见 [00_启动说明.txt](../00_启动说明.txt)，完整功能说明见 [项目 README](../README.md)。除非另有说明，文档中的命令和代码路径均以项目根目录为基准。

## 使用和开发

| 文档 | 内容 |
| --- | --- |
| [PySide6 发布与回退](pyside6-release.md) | 正式版本位置、启动与历史备份说明 |
| [Qt Quick 迁移与验证](qt-quick-migration.md) | 六阶段实现、QML 预览、依赖、离线验收与 Widgets 回退 |
| [减少开发时的重启](development-feedback.md) | 样式重载、运行时设置、离线预览及验证流程 |
| [Lock-in 控制](lockin-control.md) | Lock-in 配置、读回与测量前检查 |
| [Drag/Drive 设置](drag-drive.md) | 双 Lock-in 测量配置及信号解释 |
| [AC 文件命名](ac-filenames.md) | 数据文件名和元数据约定 |
| [ETA 校准记录](eta-calibration-2026-09-28.md) | 测量时长估算的历史校准依据 |

## 操作指南：guides

| 文档 | 内容 |
| --- | --- |
| [attoDRY1000 样品温控](guides/SAMPLE_TEMPERATURE_1000.md) | LS335 目标、加热器范围、升温速率与稳定等待 |
| [磁场相关热保护](guides/FIELD_THERMAL_COMPARISON.md) | 不同磁场下的温度边界与保护行为 |

## 仪器手册：manuals

- [APS100 magnet power supply 手册](<manuals/05_Manual APS100 magnet power supply.pdf>)

## 审计与故障记录：reviews

这些文档保留当时的证据、问题和验证结果；使用时注意记录日期及其中的修复状态。

- [Transport 累积审计记录](reviews/TRANSPORT_AUDIT.md)
- [2026-09-08 Gate Scan 故障](reviews/BFIELD_FAILURE_20260908.md)
- [2026-09-07 B-field 后续审计](reviews/BFIELD_FOLLOWUP_AUDIT_20260907.md)
- [2026-09-07 B-field 日志复查](reviews/BFIELD_RECENT_LOG_REVIEW_20260907.md)

## 设计与实现计划：superpowers

- [Curve History 设计](superpowers/specs/2026-10-01-curve-history-design.md)
- [Curve History 实现](superpowers/plans/2026-10-01-curve-history.md)
- [PNG 导出](superpowers/plans/2026-10-01-png-export.md)
- [Analysis 重载](superpowers/plans/2026-10-02-analysis-reload.md)
- [Drag/Drive 实现](superpowers/plans/2026-10-02-drag-drive.md)

开发脚本在 [`tools/`](../tools/README.md)。离线测试报告在 `tmp/verification/reports/`，预览 PNG 和配套 JSON 在 `tmp/verification/previews/`；这些路径以项目根目录为基准，属于本机生成文件。`tmp/` 中的旧副本和备份不能作为正式启动入口。
