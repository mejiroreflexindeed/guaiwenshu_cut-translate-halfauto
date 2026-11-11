# completion-W06

```text
工作包：W06 质量规则与人工定位；计划版本：1.1；基线提交：1a9a0104…（W05 提交 54577c7）
需求：FR08、FR16；决策：D15、D19 相关；验证：T03（质量表部分，整组随本包结算）
里程碑：无（M2 在 W07）
```

## 实现

用户可观察的新行为（命令行与版本目录；GUI 接入在 W08）：

| 行为 | 说明 |
| --- | --- |
| 每个机器版本都带复核报告 | `revisions/machine/<id>/` 新增 `review.html`（离线静态页）、`review.csv`（UTF-8 BOM，固定 12 列）、`quality_scope.json`（哪些规则检查了、哪些没检查及原因、哪些被设置关闭），`quality.json` 现在是第 11.2 节完整规则表的结果。四个文件都登记在 manifest 里按哈希校验 |
| 人工稿登记时同样生成报告 | `review import` / `source import` 的版本目录里同样有这四个文件；能与基础版本确定对应的条目，在报告里把日文源文并排列出（只取 exact 对应的角色；candidate 对应只作提示并标 `partial`；其余 `unmapped`，不猜） |
| 命令返回值给出报告位置 | `review import` / `source import` 的 `ids.report_files` 指向 `review.html`、`review.csv`、`quality_scope.json` |
| 结构错误不提交 | 机器流水线最后一步遇到任何 ERROR（QC01–QC06、QC17 等）不产生版本，报告留在本次尝试目录 `quality_refused/` 里供查看；错误码按规则归类（QC05 → `SOURCE_COVERAGE_ERROR`，QC04/QC17 → `TRANSLATION_IDENTITY_ERROR`，其余 → `SUBTITLE_INVALID`） |
| 提示不拦截、不改稿 | WARNING / INFO 只写进报告；没有任何规则改字、合并、删除、移动字幕或平移全轨 |
| `review frames` 按需导出边界帧 | `review frames --data-root … --project … --revision … (--issue <issue_id> \| --at-ms 1000,2000) --destination <新目录>`：每个时间点导出"当时在屏幕上的那一帧"和"它前一帧"两张 PNG，文件名与 `frames.json` 标注 ffmpeg 解码出的实际 PTS 和时间基；目录已存在即 `OUTPUT_EXISTS`，媒体时钟未验证即 `MEDIA_CLOCK_UNKNOWN`；只读原视频 |

规则表（`quality.evaluate`，计划第 11.2 节；阈值取 `AppConfig.quality`，开发方没有为减少提示改动任何默认值）：

| 规则 | 本包实现 | 证据不存在时 |
| --- | --- | --- |
| QC01/QC02 | 解析器按行号给出的问题，ERROR | — |
| QC03 | 超出媒体时长超过容差 ERROR；容差内（SRT 1 ms、ASS 10 ms）INFO，原值保留 | 没有绑定媒体：未检查（QC21 记 INFO 说明） |
| QC04 | 分段/译文内部编号重复、译文对应的源版本不一致、原文哈希不一致（陈旧译文）、机器字幕条数或时间与分段不一致 | 人工文件：未检查（身份在登记时按文件哈希核对） |
| QC05 | 机器源每个识别句的非空白字符恰好归属一条字幕（与 v8 `audit_segment_records` 同一条件） | 没有语音原文：未检查 |
| QC06 | 机器字幕编号不连续/未排序 ERROR；人工文件 INFO，不重排 | — |
| QC07/QC08 | 默认 500/10000 ms、15 字/秒；ASS 标签不计字符 | 阈值为 null 即关闭，`disabled_rules` 列出 |
| QC09 | 同角色同层 WARNING；不同角色或不同层 INFO；无角色的外部文件 WARNING（`verification=unknown`）。两条都原样保留 | — |
| QC10/QC11/QC12 | 只对登记过类型的分数套阈值（ASR `whisper_token_probability` < 0.50；OCR `ocr_confidence_0_1` < 0.80）；其他类型或没有分数 → 每个后端汇总一条 QC12 INFO，不给逐条字幕编分数 | 现在的 Magia 适配不出分数（B03），真实运行里 OCR 永远是 QC12 |
| QC13 | 相邻画面文字相同或互为前缀、间隔 ≤ 200 ms（只在比较视图里做 NFKC+去空白）；附边界时间，可直接 `review frames --issue` | 没有 OCR 证据：未检查 |
| QC14 | v8 的 `TEXT_CONFLICT` / `TEXT_ALIGNMENT_AMBIGUOUS` / `VISUAL_MAPPING_UNRESOLVED`，ASR 与 OCR 两栏各带时间并列，不判断谁对；未进入字幕的画面文字单独列出 | 人工源：未检查 |
| QC15 | 词时钟被拒/缺失/有缺口，或时间来源是整句包络，显示 `timing_source` 与原始证据 | 人工源：未检查 |
| QC16 | 有语音活动区间时，区间内 ≥ 1000 ms 无语音字幕覆盖 WARNING | 当前 ASR 证据没有活动区间 → INFO + `unknown_capabilities: speech_activity`，不宣称没有漏句 |
| QC17 | 缺译文、空译文对应非空原文、多出的译文，ERROR | — |
| QC18 | 现有译文标记 `TRANSLATION_TERM_MISSING`（附缺的译名）、`TRANSLATION_JAPANESE_PRESENT` | 人工改过的译文不套用机器标记 |
| QC19 | 现有标记 `[待确认]`、`AMBIGUOUS_TEXT_OCCURRENCE`、`UNMAPPED_SPEECH`、`VISUAL_TEXT_MISSING`、`VISUAL_ONLY` 及其他带 `CHECK` 的标记；"只识别画面文字"模式下 `VISUAL_ONLY` 是模式事实，汇总成一条 INFO（Q-W04-3 结算） | — |
| QC20 | 基础版本已更新 INFO、来源映射不完整 INFO、样式表变化 WARNING（W05 已有） | 不是人工文件：未检查 |
| QC21 | 绑定媒体与目标媒体不同 ERROR；接口已备好，导出时由 W07 传入目标媒体 | 版本内：未检查 |
| QC22 | 视频导出缺字体/字形 ERROR，只导出字幕 WARNING；接口已备好，由 W07 的字体检查结果驱动 | 版本内：未检查 |
| QC23 | 没有任何可显示文字 WARNING，不造占位字幕 | — |

复核交付（计划第 11.3 节）：

- `review.csv`：列固定为 `revision_id,cue_id,start,end,role,source_text,translation_text,rule_ids,severity,message,source_ref,provenance_status`；每条显示字幕一行（多条问题合并，取最严重级别），不属于任何字幕的问题各占一行；全部单元格加引号；首个非空白字符为 `= + - @` 或以制表符/回车开头的单元格前加英文单引号——只在展示文件里，JSON 与字幕文件保留原文。CSV 不是回流格式，改它不会改动任何数据。
- `review.html`：内容安全策略禁止脚本与网络（`default-src 'none'`）；所有正文与属性转义；链接全部是相对路径并做百分号编码（机器 SRT/ASS、quality.json、review.csv、本次使用的日文源字幕，以及来源明确标注的 ASR/OCR 参考字幕）；列出每条规则这次是"已检查 / 未检查（原因） / 已关闭"。没有在线字体、CDN 或统计。
- 边界帧：按需导出（不放进不可变的版本目录），见上表。帧标签取 ffmpeg `showinfo` 报告的解码帧 PTS，并与包探测得到的呈现 PTS 列表核对；不一致即 `EXPORT_VALIDATION_FAILED`，从不贴推算的时间。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/quality.py` | 完整规则表 `evaluate()`（返回报告、规则范围、逐条表）、`check_quality()` 保持计划接口；`render_review_csv` / `render_review_html` / `review_files` |
| `app/tools/upgrade/stages.py` | QUALITY_AND_COMMIT 改用完整规则表，写入报告四件套，结构错误不提交；只在规则版本与阈值都相同时复用已有机器版本 |
| `app/tools/upgrade/runner.py` | `STAGE_VERSIONS["QUALITY_AND_COMMIT"] = 2`（IF-28 结算：旧的质量阶段缓存不再复用） |
| `app/tools/upgrade/revisions.py` | 登记人工稿时生成报告四件套（manifest `extensions.report_refs`，参与哈希校验）；逐条来源只取对应关系已确定的部分 |
| `app/tools/upgrade/media.py` | `boundary_frame_plan`、`export_boundary_frames` |
| `app/tools/upgrade/cli.py` | `review frames`；导入结果的 `report_files` |
| `tests/upgrade_quality_test.py`（新） | 18 个用例（见下） |
| `tests/upgrade_stages_test.py` | 两处断言从"W06 规则未检查"改为 W06 的实际结果 |

未改动：旧 GUI、旧流水线、v8 分段算法、`subtitle_axis_align.py`（`normalize_text` 默认语义不变）、`contracts.py` 与 `schemas/`（报告记录结构没有变）、旧 22 个测试。

## 验证

| 项（计划 W06 验收 / T03） | 状态 | 证据 |
| --- | --- | --- |
| 一组表驱动输入覆盖每个硬错类别 | PASS | `HardErrorTable`：QC03（超界 ERROR、1 ms 量化 INFO）、QC04（重复编号、译文源版本不符、陈旧原文哈希）、QC05、QC06、QC17、QC21、QC22 逐行触发且版本判为 invalid；QC01/QC02 用人工文件验证并带行号；ASS 10 ms 容差内为 INFO 且原值保留 |
| 每个软警示类别 | PASS | `SoftHintTable.test_each_soft_hint_class_warns_without_changing_a_subtitle`：QC07（过短、过长）、QC08、QC09、QC10、QC11、QC12、QC13、QC14、QC15、QC16（WARNING 与 unknown 两种）、QC18（两种标记）、QC19、QC22（字幕出口）、QC23 逐行触发；每行都核对版本仍为 valid、显示文字不变、`evaluate()` 没有改动输入 |
| OCR/ASR 不同文字不会被改成同文 | PASS | `test_ocr_and_asr_texts_are_shown_side_by_side_and_never_made_the_same` |
| 无分数显示 unknown | PASS | `test_without_a_score_…`（每个后端一条汇总，逐条表里没有分数字段）、`test_a_score_of_another_kind_…`（logprob 不套概率阈值） |
| 合理跨角色重叠不删除 | PASS | `test_simultaneous_display_of_different_roles_or_layers_is_kept_and_only_informed`（不同角色、不同层为 INFO；无角色外部文件为 unknown WARNING；条数不变） |
| CSV/HTML 内容安全 | PASS | `DeliverableSafety`：公式前缀只出现在 CSV 展示里；`<script>`、属性注入、`</td>` 全部转义；无 http(s) 引用；有内容安全策略；23 条规则都有说明 |
| 帧标签是实际 PTS | PASS | `BoundaryFrames`（真实 ffmpeg/ffprobe）：恒定帧率 MP4、可变帧率 MP4、视频起点 1.4 s 的 MKV 三种时钟，导出帧的 PTS 都在呈现帧列表里、前后两帧相邻、文件是 PNG；已存在目录与未验证时钟被拒且不留文件。`PipelineReports.test_boundary_frames_come_from_the_command_line_…` 经真实子进程走 `review frames` |
| 阈值不由开发方改动；null 只关软提示 | PASS | `test_thresholds_set_to_null_turn_hints_off_but_the_hard_rules_still_run` |
| 真实流水线产出报告四件套 | PASS | `upgrade_stages_test.py` 的 combined 用例（文件登记在 manifest、CSV 列头、HTML 指向 ASR 参考字幕）；`PipelineReports.test_a_registered_human_file_gets_its_own_report_…`（人工稿报告里 exact 条目的日文源文与角色来自基础分段） |
| 真实识别引擎输出上的报告 | NOT_RUN | B01/B03：执行机没有本地引擎的精确构建；T07 属 W09 |
| QC21/QC22 的导出时触发 | NOT_RUN（本包只验证接口） | 导出属 W07 |

全套：旧 22 个 + 新 14 个测试文件，36/36 通过（Windows、CPython 3.10.6 隔离 venv，2026-11-10T13:25Z UTC）。证据文件：`LOCAL_WORKSPACE/roast-w06-tests-summary.json`。

实施中由测试发现并修正的问题：

1. 没有观测值的说明性问题（例如"没有绑定媒体"）最初写成 `observed: null`，不符合 Issue 记录；改为空对象。
2. 边界帧最初用 `-seek_timestamp`，在起点不为 0 的 MKV 上 ffmpeg 解出的是 2.8 s 的帧而不是 1.4 s；改为按文件起始时间换算的普通输入寻址，并始终用 `showinfo` 核对，不一致就拒绝。
3. 旧的质量阶段在规则表改变后仍会复用旧版本；改为只在规则版本与阈值相同时复用。

## 数据保护

没有修改旧业务文件。测试只写临时目录。原视频只读（边界帧导出不写版本目录，只写用户指定的新目录与项目日志）。没有访问外部网站，没有安装软件。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| "未确认译名""未完句"没有检测 | 代码里没有对应标记；本版不新增语义检测（Q-W06-1/2） | QC18/QC19 只基于现有标记；B06 |
| OCR 分数类型 `ocr_confidence_0_1` 尚无后端产出 | Magia 未导出分数（B03） | 真实运行中 QC10 不会出现，只有 QC12 汇总 |
| QC16 在真实运行中只能是 unknown | ASR 证据没有活动区间；本版不新增 VAD/声纹 | 报告明确写"不能判断是否漏句" |
| 边界帧不进版本目录 | 版本只创建一次、不可变；计划没有规定命令与目录（CO-CAND-006） | 需负责人确认命令形态 |
| GUI 里"查看复核报告" | 属 W08 | 目前用文件管理器打开版本目录里的 `review.html` |
| 3.13 下未运行 | B02 | 同前 |

## 回滚

撤回本提交即回到 W05（54577c7）。新版本目录里多出的 `review.csv` / `review.html` / `quality_scope.json` 可以忽略或删除，字幕文件本身不受影响；`STAGE_VERSIONS` 回到 1 后旧缓存重新生效。计划第 W06 节规定的边界：停用报告展示后原字幕照常可用；硬性身份与数据保护检查不得为通过失败而关闭。

## 下一包

W07 独立字幕/视频出口（依赖 W03、W05、W06 已满足）：`roast export`，按版本原格式导出、显式 SRT 派生、MP4 硬字幕与 MKV 软字幕、字体检查（驱动 QC22）、目标媒体核对（驱动 QC21）。
