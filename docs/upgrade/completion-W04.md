# completion-W04

```text
工作包：W04 主记录、词典、翻译和机器字幕；计划版本：1.1；基线提交：1a9a0104…（W03 提交 7de0452）
需求：FR03、FR04、FR07；决策：D02、D04、D05、D06、D07、D15 相关；验证：T03（分段契约与机器格式部分）、T04（翻译/词典协议）
里程碑：M1 —— 新框架第一次能够交付可编辑的机器字幕（SRT + ASS + 来源文件 + 结构性质量报告）
```

## 实现

用户可观察的新行为（命令行；GUI 接入在 W08）：

| 行为 | 说明 |
| --- | --- |
| `queue run` 跑完全部八个阶段 | INPUT → MEDIA_PREP → OCR → ASR → SOURCE → TRANSLATION → SUBTITLE_BUILD → QUALITY_AND_COMMIT；请求状态变为 `COMPLETED`，返回值里列出每个请求的 `machine_revision_dir` |
| 机器字幕版本 | `projects/<pid>/revisions/machine/<revision_id>/`：`machine.zh.srt`、`machine.zh.ass`、`translations.json`（逐条译文及其请求/响应证据）、`quality.json`、`quality_scope.json`、原始 HTTP 交换、毫秒旁路、样式说明；一次提交、manifest 列出全部文件哈希 |
| 源字幕版本 | `revisions/source/<revision_id>/`：规范日文源 SRT（`source.ja.srt`）、v2 分段记录（`segments.json`）、原样保存的 v8 记录（`segments.v8.json`）、ASR 参考字幕（`recognition/reference.asr.ja.srt`）、OCR 参考字幕（`recognition/reference.ocr.ja.srt`）、两类识别证据副本、机器时间轴 ASS；不再把 ASR 叫 `jp_ocr` |
| 四种模式都能出字幕 | combined 按 v8 mixed/screen/speech；speech_only 不需要画面输入；visual_only 以画面事件为源（role=screen，不要求语音覆盖）；supplied_source 读取已登记的源字幕版本，不重跑识别 |
| 空结果是明确结果 | 识别为空时产出合法的空 SRT（只有 BOM）与无 Dialogue 的 ASS，质量报告记 `QC23_EMPTY`，不启动翻译服务，不造占位字幕 |
| 词典先查再用 | 提交时把前置/GPT/后置词典、用户术语表、风格提示（需要时含 VoiceTransl 指引）按字节复制为词典版本；不支持的条目逐行列出文件、行号、原文、原因；`submit --accept-dictionary-warnings` 才继续用受支持子集，同一内容只问一次 |
| 翻译服务自有 | llama-server 由程序以旧命令行启动、健康就绪后才用、阶段结束即关闭；端口被别的服务占用报 `SERVER_PORT_CONFLICT`，不接管、不关闭它 |
| 翻译可恢复 | 每条译文一落地就写检查点；失败或中断后 `resume` 只补翻剩余条目，不重复请求；上下文溢出报 `CONTEXT_EXCEEDED` 并保存完整待译正文 |
| 缺翻译引擎在开跑前就说 | INPUT 阶段的资源预检现在也包括 `llama-server` 与 `sakura-model` 两个角色 |

机制（计划第 6.5、6.7、7.5、8.3–8.5、10.1、10.3 节）：

- **分段**：`subtitle_segments.build_segments` / `audit_segment_records` 原样调用，输入与旧流水线一致（ASR 父句 → Cue，OCR 事件 → Cue，有效的原生词时钟 → word_times）。外层只做转换：v8 条目 → `Segment`（公开主键 `(source_revision_id, cue_id)`，`cue-NNNNNN`；旧 id 保存在 `legacy_cue_id`），范围指向 SpeechEvidence 父句 id，视觉 id 指向 VisualEvidence 事件 id；角色由内容来源决定（有语音范围且关联画面页 → mixed，仅语音 → speech，仅画面 → screen）；v8 标记原样保留，父句词时钟被拒时追加 `WORD_CLOCK_REJECTED`/`CHECK`（计划 8.1）。覆盖审计失败 → `SOURCE_COVERAGE_ERROR`；超出媒体末尾（容差 1 ms）→ `SUBTITLE_INVALID`（QC03），不钳制。
- **词典**：`voice_dictionary` 仍是唯一解释者。诊断逐行重放解析器的判断并与解析器实际结果核对（条目数、不支持数必须一致，否则拒绝描述）；跨文件同源不同目标列 warning，不合并、不改优先级（术语表优先于 GPT 词典，前置词典改源文，后置词典改译文——与旧代码一致）。词典版本按内容寻址，复制字节，不重新序列化。
- **翻译**：沿用 `sakura_client.plain_translation_payload` 与 `read_chat_stream`；多行 cue 单独请求、单行 cue 成批；响应完整且行数一致才按序对应，否则保存原响应并拆成逐 cue 请求（整批作参考）；每单元最多 3 次（首次 + 2 次重试），外层不再乘；单 cue 仍失败则停止阶段。连接超时固定 10 秒、生成不设截止、60 秒无新内容只发 `translation_waiting` 事件；取消时从另一线程中止 socket（Windows 上仅 shutdown 不能唤醒阻塞的 recv，所以同时关闭句柄）。译后处理沿用旧函数：后置词典、术语就地替换、缺术语/残留假名/`[待确认]` 只打标记，不补词。
- **机器格式**：SRT = UTF-8 BOM、CRLF、从 1 连续编号、毫秒；ASS = BOM、CRLF、最近 10 ms 正数半向上；舍入后时长非正 → `TIME_PRECISION_COLLAPSE`，原毫秒存 `timing.ms.json`。样式沿用源版本携带的机器时间轴 ASS（样式表 + 关联 OCR 事件的 Layer/Style/Name/Margin/Effect）；没有时间轴时用第 7.5 节固定基础样式。写出后用旧解析器回读核对（条数、时间、文本、编号、边界）。
- **提交门禁**：QUALITY_AND_COMMIT 在 W04 只做结构/身份门禁（QC01–QC06、QC17、QC23）并提交不可变 MachineRevision；`quality_scope.json` 列明哪些规则本构建没有评估（QC07–QC16、QC18–QC22 属 W06）。失败的尝试不产生版本，也不改指针；同一 run 内重复提交同内容复用已有版本。
- **缓存**：各阶段只声明真实依赖——换时间轴模式复用 OCR/ASR、重做 SOURCE 及下游；换翻译参数复用识别与源版本、只重做 TRANSLATION 及下游；supplied_source 指向同一源版本时连翻译都复用（验证：无新请求、无新服务进程、同一 machine_revision_id）。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/source.py`（新） | v8 接入：证据 → 旧输入、`build_source`、Segment 转换、边界检查、SourceRevision 提交/加载/幂等查找、参考字幕 |
| `app/tools/upgrade/dictionary.py`（新） | 词典输入解析（显式路径 / 迁移键 / 内置默认）、逐行诊断、内容寻址的词典版本、确认记录、翻译用加载 |
| `app/tools/upgrade/translation.py`（新） | `translate(TranslationRequest)`、自有 llama-server、传输层、逐 cue 检查点、批次/单句规则、译后处理与标记、翻译签名 |
| `app/tools/upgrade/subtitles.py`（新） | 机器 SRT/ASS 序列化、模板解析、基础样式、回读核对 |
| `app/tools/upgrade/stages.py` | 新增 SOURCE / TRANSLATION / SUBTITLE_BUILD / QUALITY_AND_COMMIT 四个阶段；翻译角色进入 INPUT 预检；supplied_source 从源版本取媒体 |
| `app/tools/upgrade/stage_registry.py` | 注册八个阶段 |
| `app/tools/upgrade/runner.py` | 缓存命中时沿用已提交版本的 `machine_revision_id`；`run_queue` 返回每个请求的结果与版本目录 |
| `app/tools/upgrade/contracts.py` | 新增 `SegmentSet`、`TranslationSet`、`DictionaryFile`、`DictionaryRevision`、`TranslationRequest`（共 60 种）；Segment 范围非空检查 |
| `app/tools/upgrade/cli.py` | `submit` 的词典预检与确认；`--project --source-revision --mode supplied_source` 不带视频时从源版本取媒体；`inbox intake` 同样冻结词典版本 |
| `app/tools/upgrade/config.py` | 旧 `resources.translation_prompt/translation_glossary` 绝对路径迁移为 `dictionary.style_prompt/glossary` |
| `schemas/` | 5 个新 schema，`index.json` |
| `tests/upgrade_source_test.py`、`tests/upgrade_subtitles_test.py`、`tests/upgrade_dictionary_test.py`、`tests/upgrade_translation_test.py`（新） | 39 个用例 |
| `tests/upgrade_stages_test.py` | 改为八阶段端到端（14 个用例，原 10 个） |
| `tests/support/w04.py`、`tests/support/fake_llama_server.py`、`tests/fixtures/v8_canonical_slice.json`（新）；`tests/support/w03.py`、`tests/upgrade_contracts_test.py` | 夹具：证据/分段集/词典文件构造、回环模型服务替身、冻结的 v8 规范输出；资源种类表；导入隔离覆盖新模块 |

未改动：旧 GUI、`roast_pipeline.py`、`voicetransl_adapter.py`、`sakura_client.py`、`voice_dictionary.py`、`subtitle_segments.py`、`subtitle_axis_align.py`、旧 22 个测试。新代码只调用旧模块里的纯函数。`subtitle_segments.py` 与基线提交逐字节相同（`git diff 93df262` 为空），冻结夹具就是用它生成的。

## 验证

| 项（计划 W04 验收） | 状态 | 证据 |
| --- | --- | --- |
| T03 基线 v8 fixture canonical 源范围/时间/文本保持 | PASS | `upgrade_source_test.py::test_frozen_v8_slice_is_reproduced_for_mixed_screen_and_speech`：同一输入直接调用算法的结果、冻结夹具 `tests/fixtures/v8_canonical_slice.json`、新流程存下的 v8 记录三者逐项相同（mixed/screen/speech） |
| T03 visual_only / 空源语义；OCR/ASR 不互覆 | PASS | `test_visual_only_uses_the_screen_events_without_a_speech_denominator`、`test_empty_results_are_explicit_…`、`test_ocr_and_asr_text_do_not_overwrite_each_other`；队列级 `test_empty_recognition_is_an_explicit_empty_revision_without_a_model_server` |
| T04 完整 SSE、断流、少/多行、单 cue 重试、context 溢出、同名外部 server 不接管 | PASS（回环真实 HTTP，非真实模型） | `upgrade_translation_test.py` 17 用例；台账 `T04-W04` |
| T04 pre/gpt/post/once/prefix/unsupported 行；完成条目恢复不重复请求 | PASS | `upgrade_dictionary_test.py` 7 用例；`test_single_cue_gets_three_attempts_…`（恢复后只发 1 个请求）；队列级 `test_failed_translation_leaves_no_revision_pointer_and_resumes_from_checkpoints` |
| 翻译断流/错行不提交 | PASS | 断流：HTTP 200 不算成功、部分字节留证、重试；错行：拆单句、原响应留证；失败时没有 `translations.json`、没有版本目录 |
| 三次重试没有外层倍增 | PASS | 单 cue 恰好 3 个请求后阶段停止；队列不再自动重试，`resume` 才新 attempt |
| 词典遗漏不句尾补词 | PASS | `test_pre_glossary_post_and_review_flags_follow_the_legacy_rules`：缺术语的译文原样保留并打 `TRANSLATION_TERM_MISSING` |
| 无剪映也产生完整字幕输出 | PASS | `test_combined_runs_every_stage_and_delivers_an_editable_machine_revision`：没有任何 NLE 步骤，`machine.zh.srt/.ass` 经哈希提交，`queue run` 直接返回目录 |
| 失败的新 revision 不得成为当前指针 | PASS | 翻译失败后 `task.machine_revision_id` 为空、`project.machine_revision_ids` 为空、`revisions/machine/` 无目录 |
| 机器 SRT/ASS 格式（6.7）与基础样式（7.5） | PASS | `upgrade_subtitles_test.py` 5 用例（BOM/CRLF/编号、10 ms 半向上、TIME_PRECISION_COLLAPSE、模板样式继承、回读核对） |
| 真实 llama-server + Sakura-GalTransl-7B-v3.7 | NOT_RUN | 执行机没有（B01）；协议部分用回环替身验证；真实引擎整体验证按计划留 W09 |

全套：旧 22 个 + 新 12 个测试文件，34/34 通过（Windows、CPython 3.10.6 隔离 venv；两次连续运行结果相同）。证据文件：`LOCAL_WORKSPACE/roast-w04-tests-summary.json`。运行方式与 W03 相同：

```powershell
python scripts/run_tests.py --ffmpeg <ffmpeg.exe> --ffprobe <ffprobe.exe>
```

没有 ffprobe 的环境里 `upgrade_stages_test.py` 的 14 个用例显示为 skipped；翻译/词典/分段/格式四组不依赖媒体工具。

实施中由测试发现并修正的问题：

1. Windows 上从另一线程 `shutdown()` 不会唤醒阻塞在 `recv` 的流读取，取消要等模型发完才生效；改为同时关闭 socket 句柄后取消在 0.2 秒内生效。
2. supplied_source 的机器 ASS 一度退回基础样式（该 run 没有 OCR 阶段），导致同一源版本的两次运行产出不同字幕、无法复用；改为把机器时间轴 ASS 存入源版本随版本携带。

## 数据保护

未修改任何旧业务文件。测试只写临时目录；词典文件由测试现场生成，内置默认词典（`app/config/*.txt|json`）只读复制，未改动。回环 HTTP 只连 127.0.0.1，未访问外部网站。对外部工具目录只读。未写用户文档目录，未安装任何软件。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 真实 llama-server / Sakura 模型未运行 | 执行机没有（B01） | 服务启动参数、`/health`、`/v1/models` 的模型名核对、SSE 格式、上下文溢出错误形态按旧代码与 llama.cpp 源码假设（`decisions.md` Q-W04-2），留 W09 实测 |
| QUALITY_AND_COMMIT 只有结构门禁 | 软规则、review.csv/html 属 W06 | 版本目录里的 `quality_scope.json` 明示未评估的规则；分段/翻译标记已保留供 W06 使用 |
| supplied_source 目前只能指向机器生成的源版本 | 人工源字幕导入（`source import`）属 W05 | 路径与媒体绑定检查已就绪，W05 只需产出 origin=human 的 SourceRevision |
| OCR 来源时间的边界容差 | 画面事件时间本身是 10 ms 量化的；规范对 canonical 值只给 1 ms 容差 | 若真实 Magia 的末尾事件超出媒体末尾 2–9 ms，SOURCE 会按 QC03 拒绝（Q-W04-1，W09 用真实输出确认） |
| visual_only 的每条 cue 都带 v8 的 `VISUAL_ONLY`/`CHECK` | 标记原样保留，未按模式改写 | W06 决定 QC19 在该模式下如何对待（Q-W04-3） |
| 翻译检查点只在同一 run 内复用 | 跨 run 的部分复用是允许项不是要求项 | 相同签名的新请求从头翻译（完整结果由阶段缓存复用） |
| `workers` 只记录不生效 | 旧代码固定 `--parallel 1` 顺序执行 | 与旧版一致 |
| 3.13 下未运行 | 未安装（B02） | 同 W01 |

## 回滚

删除 `source.py`、`dictionary.py`、`translation.py`、`subtitles.py`、四个新测试文件、`tests/support/w04.py`、`tests/support/fake_llama_server.py`、`tests/fixtures/`、五个新 schema，并撤回 `stages.py`、`stage_registry.py`、`runner.py`、`contracts.py`、`cli.py`、`config.py`、`schemas/index.json`、`tests/support/w03.py`、`tests/upgrade_contracts_test.py`、`tests/upgrade_stages_test.py` 的 W04 片段，即回到 W03（7de0452）。v2 DATA_ROOT 里的 `dictionaries/`、`revisions/source|machine/`、`runs/*/checkpoints/` 可整体删除，不影响旧 `app/work` 任务；旧词典文件与模型未被改动。

## 下一包

W05 人工中文/日文修订闭环（依赖 W04 已满足）：`review create/import`、`source import`、原字节快照、对应关系与确认范围；人工源版本接入已就绪的 supplied_source 路径。
