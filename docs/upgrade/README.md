# docs/upgrade — 升级实施台账

本目录是计划 1.1（`../architecture-audit/08-complete-upgrade-plan.md`）第 4.1 节规定的 `docs/upgrade/`，保存 W00–W10 的基线、决策、验证和完成记录。每个工作包结束时提交 `completion-Wxx.md`。

| 文件 | 内容 | 建立于 |
| --- | --- | --- |
| `baseline.json` | 基线提交、全部跟踪文件 SHA-256、发布清单核对结果、旧测试基线、执行环境事实、旧测试分类 | W00 |
| `decisions.md` | 原样采纳的决策、执行环境与计划假设的差异及处理、负责人待办 | W00 |
| `traceability.json` | FR/D/QC/W/T/B/S 编号索引与对应关系 | W00 |
| `validation-ledger.json` | T01–T09 证据台账（第 14.5 节字段） | W00，逐包更新 |
| `resources-inventory.json` | 执行机外部工具/模型登记（公开版，个人路径为占位符）及默认引擎缺失项 | W00 |
| `source-origin-inventory.json` | 拟公开代码与依赖的来源/许可登记（第 19.4 节字段） | W00 |
| `license-options.md` | 主许可证选择材料，交负责人决定（B10） | W00 |
| `completion-W00.md` | W00 完成报告（第 17.2 节模板） | W00 |
| `runtime-lock.json` | 主运行时选版（3.13.17）与依赖分组/锁状态（第 12.2 节） | W01 |
| `requirements-lock-dev-3.10.txt` | 3.10 开发 venv 的 pip freeze（无哈希） | W01 |
| `completion-W01.md` | W01 完成报告 | W01 |
| `completion-W02.md` | W02 完成报告 | W02 |
| `completion-W03.md` | W03 完成报告（媒体时钟、派生媒体、两类识别接入） | W03 |
| `completion-W04.md` | W04 完成报告（源版本、词典版本、翻译、机器 SRT/ASS、结构门禁与机器版本提交；里程碑 M1） | W04 |
| `completion-W05.md` | W05 完成报告（校对副本、人工中文稿/原文改稿整文件登记、对应关系、确认、版本解析） | W05 |
| `completion-W06.md` | W06 完成报告（QC01–QC23 规则表、review.csv/review.html/quality_scope.json、按需边界帧） | W06 |
| `completion-W07.md` | W07 完成报告（字幕原字节导出与显式转换、MP4 硬字幕、MKV 软字幕、字体检查、输出校验、M2） | W07 |
| `completion-W08.md` | W08 完成报告（旧窗口新选项卡：新任务对话框、队列列表与操作、任务详情按钮、原文修订分支、资源与媒体重新定位、关窗确认；旧任务只读迁移） | W08 |
| `blocked-W09-W10.md` | W09/W10 阻塞登记（真实引擎、编辑器、干净机、许可证需负责人提供） | W09/W10 |

不入库的私有快照（`.gitignore` 排除 `*.private.json`）：`effective-config.private.json`、`resources-inventory.private.json`，保存执行机的实际绝对路径。

占位符含义沿用审计文档：`LOCAL_WORKSPACE`（执行机工作区）、`EXTERNAL_TOOLS_ROOT`（执行机外部软件/模型根目录）、`LEGACY_ROOT`（维护者旧正式目录，不在执行机）。
