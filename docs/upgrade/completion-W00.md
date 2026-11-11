# completion-W00

```text
工作包：W00 基线与执行规格冻结；计划版本：1.1；基线提交：1a9a0104defbd9881db3f9eded017ad7b1f96edc（业务代码同 93df2625…）
需求：FR01–FR16 基线；决策：D01–D20 原样采纳，未提变更单；验证：static（第 14.5 节台账已建）
```

## 实现

用户可观察的新行为：无（W00 不写功能代码）。新增文件全部在 `docs/upgrade/`，加 `.gitignore` 一行 `*.private.json`。契约未变化。

| 任务（计划 W00 第 1–5 条） | 做了什么 | 产物 |
| --- | --- | --- |
| 1 隔离 checkout、记录 HEAD、比较 fix_stage 差异 | 从上游 1a9a010 建分支 `upgrade/subtitle-workflow-v2`；执行机无 fix_stage，改为核对 `docs/source_manifest.json` 51 条发布哈希，全部一致；release_sync 布局差异按清单记录保留 | `baseline.json#git`、`#source_manifest_check` |
| 2 源码/配置/样本 hash、引擎模型身份 | 77 个跟踪文件 SHA-256；执行机可见的 ffmpeg、whisper-faster、llama.cpp server、Sakura 13B、faster-whisper large-v3 模型逐一哈希登记；D06 默认引擎在执行机全部缺失，如实登记 present=false | `baseline.json#file_hashes`、`resources-inventory.json`（公开占位符版）、`resources-inventory.private.json`（本地） |
| 3 固定 35 秒/572.636 秒任务与既有测试结果 | 登记审计 01 的历史数字（accuracy_accepted=false 等）为"复用、本机不可复核"；重跑 22 个旧测试：干净 3.10 环境 12 失败（缺 PyYAML/pywinauto），装声明依赖 + PyYAML 后 22/22 通过 | `baseline.json#legacy_evidence_registered`、`#legacy_tests`、`validation-ledger.json` LEGACY-22 两条 |
| 4 baseline/decisions/validation-ledger/resources-inventory/effective-config.private | 全部建立；effective-config 为示例配置快照（执行机无用户 local 配置），不入库 | 同名文件 |
| 5 FR/D/QC/W/T 可追踪编号、来源/许可证登记、词典待确认 | 索引建立；来源登记按 19.4 字段，全部标 self_authored 待负责人确认；许可证四个方案 + 三个问题交负责人；`dict_gpt.txt` 两条 `[待确认]` 与 glossary 一条上下文项登记到 B06 | `traceability.json`、`source-origin-inventory.json`、`license-options.md`、`baseline.json#dictionary_pending_entries` |

## 验证

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 证据路径存在 | PASS | `docs/upgrade/` 8 个公开文件 + 2 个私有文件 |
| 基线不称语义已通过 | PASS | baseline/ledger 均标 accuracy_accepted=false、"未验" |
| 源码/配置/工具来源可定位 | PASS（含 unknown） | source-origin-inventory、resources-inventory；缺失资源列 upstream_url，Magia 上游留 W01 核实 |
| 没有用户目录被写入 | PASS | 只写仓库 `docs/upgrade/`、工作区 venv 与日志、仓库内被忽略的 `test-results/`、`app/output/`（旧测试自身产物） |
| 旧测试基线 | PASS 22/22（venv）；FAIL 12（无 PyYAML） | `validation-ledger.json` LEGACY-22 / LEGACY-22-NOYAML |
| T01–T09 | NOT_RUN / BLOCKED | 见台账；T07/T08/T09 受 B01/B02/B05/B08/B09 阻塞 |

证据类型 static/fixture；build hash 1a9a010；资源清单 sha 记录在 `validation-ledger.json#resource_manifest_sha`。

## 数据保护

未修改任何旧文件（`git status` 只有新增 `docs/upgrade/` 与 `.gitignore` 追加）。未触碰原媒体、旧词典、模型、D 盘目录。77 个跟踪文件哈希已冻结在 baseline.json，后续工作包据此比较。

## 未完成

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| fix_stage AST 独立复核 | 执行机无 fix_stage（ENV-01） | 仅依赖清单采信；不阻塞 W01 |
| S01/S02 当前 hash/时长核对 | 素材不在执行机（ENV-02） | W09 |
| 默认引擎来源闭合（B01） | 执行机无 whisper.cpp/Magia/llama-server/Sakura 3.7 | W03 真实调用、W09 |
| Python 3.13（B02） | 未安装，需授权 | W01 冻结包部分 |
| 许可证决定（B10） | 等负责人回答 `license-options.md` | W10 |
| 推送与 PR | 执行账号只有读权限（ENV-05） | 交付方式 |

## 回滚

丢弃分支 `upgrade/subtitle-workflow-v2` 和 `docs/upgrade/` 即回到 1a9a010；工作区 venv/日志可直接删除；旧项目与上游仓库未改。

## 下一包

W01（依赖 W00 已满足）：contracts/resource_catalog/配置版本读写/不可变 revision 提交原语/数据目录写入检查/依赖锁与 PyYAML 显式声明；3.13 版本按 12.2 规则选定并单独申请安装授权；T01。
