# W09 / W10 阻塞登记（2026-11-11）

```text
工作包：W09 真引擎、编辑器和便携候选验收；W10 最终交付、GitHub 开源发布准备和回滚材料
计划版本：1.1；基线提交：1a9a0104…（W08 之后的分支 upgrade/subtitle-workflow-v2）
状态：BLOCKED（不是完成记录；没有任何一项被标为通过）
```

W00–W08 的代码与测试都在本执行机上完成，但 W09/W10 的验收按计划必须用真实资源完成，本执行机不具备，也不能用替身或合成素材冒充。下表列出每项需要什么、谁来提供、到位后从哪里接着做。

## W09 需要的东西

| 验收项（计划 W09 / 14.3） | 本机现状 | 阻塞 | 到位后怎么做 |
| --- | --- | --- | --- |
| T07 真实引擎：短片两种既有模式、一个纯画面样本、一次长片全流程与中断恢复 | 没有计划指定的 whisper.cpp（whisper-cli + ggml-large-v3 + silero VAD）、MagiaTimeline、llama-server + Sakura-GalTransl-7B-v3.7 的精确构建；测试里这些都是进程边界上的替身 | B01、B03、B04、B08 | `resources register` 登记真实资源 → 用 S01/S02 跑 `submit` + `queue run`（combined、speech_only、visual_only）→ 中途 `queue cancel` 后 `resume` → 证据集中放到独立 acceptance 目录，人工核对语义后登记 T07 |
| T08 实际人工工作流：Subtitle Edit 改字/拖时间/拆并/保存重开/登记导出；Aegisub 保存重开并实际渲染比对层与样式 | 没有安装 Subtitle Edit、Aegisub | B05 | 安装指定版本 → 在“新版项目与校对”选项卡里：创建校对副本 → 用编辑器打开 → 保存 → 登记校对稿 → 标记人工确认 → 导出字幕/视频；Aegisub 渲染截图与成片同帧比对 |
| T09 便携候选：one-dir 冻结包、冻结 worker、不使用开发者 Python、移动路径与无开发环境验证 | 只有隔离 venv（CPython 3.10.6）；没有 3.13（W01 选定）与冻结工具；没有干净 Windows | B02、B09 | 授权安装 3.13 与冻结工具 → 生成 one-dir 候选 → 在干净机上从移动后的路径运行全部命令 |
| 本机 CPU/GPU profile 与峰值 | 未测（真实引擎缺） | B04 | 随 T07 记录 |

## W10 需要的东西

| 项 | 阻塞 | 说明 |
| --- | --- | --- |
| 主项目许可证 | B10 | 负责人回答 `license-options.md` 第 5 节的三个问题之前，不能生成 LICENSE、第三方通知和公开源码白名单，也不能宣称“开源发布准备完成” |
| 实际分发内容的来源与许可 | B10、B07 | 包括字体（MKV 附件目前一律不附）与任何捆绑的第三方二进制 |
| 依赖 W09 通过 | — | W10 的交付清单、已测矩阵都以 W09 的真实证据为准 |

## 已经可以交给负责人的东西

- 分支 `upgrade/subtitle-workflow-v2` 上 W00–W08 的代码、测试与每包完成记录（`docs/upgrade/completion-W00.md` … `completion-W08.md`）。
- 决定与变更候选：`docs/upgrade/decisions.md`（CO-CAND-001…008 需要负责人逐条确认）。
- 验证台账 `docs/upgrade/validation-ledger.json`：T01–T06 的通过项都标明了证据类型（夹具/合成素材），T07–T09 保持 NOT_RUN。

## 回滚

本登记不改任何代码。W09/W10 未开始，没有候选包需要回滚。
