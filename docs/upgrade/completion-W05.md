# completion-W05

```text
工作包：W05 人工中文/日文修订闭环；计划版本：1.1；基线提交：1a9a0104…（W04 提交 b369044）
需求：FR05、FR06、FR07、FR10（登记与版本选择部分；导出属 W07）；决策：D03、D08、D09、D10、D14、D19 相关；验证：T05（本包负责的部分）
里程碑：无（M2 在 W07）
```

## 实现

用户可观察的新行为（命令行；GUI 接入在 W08）：

| 行为 | 说明 |
| --- | --- |
| `review create` 建立校对副本 | 把选定版本的一个字幕文件按字节复制到 `reviews/sessions/<session_id>/edit.srt\|ass`，旁边是 `base.json`（基础版本、基础文件哈希、媒体、格式）。交给外部编辑器的只有这份副本；`revisions/` 里的已提交文件从不作为工作文件。版本没有该格式时报 `NEEDS_SELECTION`，不现场转换 |
| `review import` 登记人工中文稿 | 把保存好的 SRT/ASS **原字节**登记为新的 `revisions/review/<revision_id>/`（`original.srt\|ass`、只读解析结果 `view.json`、对应关系 `correspondences.json`、`quality.json`、manifest）。不转码、不规范化、不重排、不重写；每次登记都是新版本，已有版本不动 |
| `source import` 登记人工日文源字幕 | 同样的原字节快照协议，产出 `origin=human` 的源版本；分段取用户文件里的文字、时间和拆并，不带任何识别坐标。返回值给出下一步命令（`submit --mode supplied_source --source-revision <id>`），本命令自身不排队、不翻译 |
| `review attest` 人工确认 | 用户对某个版本的某个字幕文件确认 `text` / `timing` / `style` 中明确列出的范围；确认绑定到该文件哈希与媒体。SRT 不接受 `style`；没有 `all` / `all-auto`；机器版本有两个文件时必须用 `--format` 指明确认的是哪一个 |
| 登记 ≠ 已审 | 新登记的人工稿状态是 `PARTIAL`、确认范围为空；SRT 要 text+timing、ASS 要 text+timing+style 才是 `REVIEWED`；机器版本默认 `UNREVIEWED` |
| 有问题的文件有确定去处 | 结构错误（解析失败、时间倒置或为零、超出片尾）→ `SUBTITLE_INVALID`，文件原字节和报告留在 `.pending/<id>/` 作候选（`candidate.json` 标 `NOT_COMMITTED`），不产生版本、不改索引；软提示不阻止登记 |
| 文件还在被写 | 编辑器占用或复制前后文件有变化 → `REVIEW_FILE_BUSY`（退出码 7，可重试），不登记、不循环等待；保存完成后再执行同一命令即可 |
| 媒体不符 | 校对副本/基础版本属于另一个媒体 → `MEDIA_CHANGED`；媒体 id 不存在或视频文件不在原处 → `INPUT_MISSING`；视频内容被换过 → `MEDIA_CHANGED`（带期望/实测哈希）。都不登记任何东西 |
| 机器结果更新后人工稿不动 | 重新识别、换模式、换词典都只产生新的机器版本；已登记人工稿的路径、哈希、媒体绑定、确认范围全部不变，解析它时附提示"有新的机器版本，当前人工稿基于旧版本"，仍可据它建立校对副本。没有自动合并，也没有自动变基 |
| 参数错误也是一份 JSON | 未知命令/选项（例如旧的 `--zh-file`）、缺必填项、非法取值：stdout 一份 `OperationResult`（`operation=arguments`，`CONFIG_UNSUPPORTED_COMMAND`），退出码 2；`--help` 不变 |

机制（计划第 3.2、3.3、4.3、6.6、6.7、9.1–9.4、11、12.1 节）：

- **只读解析**（`subtitles.parse_view`，已在 7b146e9 提交）：UTF-8（带/不带 BOM）、有 BOM 的 UTF-16 LE/BE 直接识别；其他编码必须由用户给出 Python codec（`--encoding`），严格解码，不用 `errors=ignore/replace`，不猜。SRT 编号只作展示；ASS 按文件自己的 `Format:` 行保留每个字段的原文（自定义字段、Name、Margin、Effect、覆盖标签、绘图命令），显示文字另行给出（`\N`/`\n` 换行、`\h` 不换行空格、去标签）。解析器不修任何东西，问题按行号报 `QC01_PARSE` / `QC02_TIME`。旧 `subtitle_axis_align` 的宽松解析器没有用于人工文件，`normalize_text` 的默认语义没有改。
- **快照**（9.2 步骤 2）：读一次字节写进 `.pending/<id>/original.<ext>`，复制前、复制后、哈希后三次比对大小与修改时间，并比对副本与源文件哈希；任何不一致或 `PermissionError` 都是 `REVIEW_FILE_BUSY`。提交前再核对一次副本哈希。源文件从不被写。
- **对应关系**（9.2 步骤 4，轻量）：只有格式相同、整条内容（ASS 为除起止时间外的全部事件字段原文）与起止时间都相同、且在两边都唯一时才记 `exact`；同时间改了字、同内容改了时间、相同但不唯一的一律是 `candidate`，仅供人看；其余 `unmapped`。没有基础版本（外来文件）或格式不同 → 全部 `unmapped`。版本的 `provenance_status`：全部 exact 且基础版本每条都被对上 → `exact`；有 exact → `partial`；否则 `unmapped`。候选行不用于配对、重翻或覆盖。
- **样式差异**：ASS 的样式表/脚本信息与基础版本不同（新增、删除、修改的样式名，变动的脚本信息键）列为 `QC20_REVIEW_BASE` 的 WARNING，由人判断是有意改样式还是编辑器丢了样式；程序不恢复、不合并样式。
- **人工源版本**（9.4）：每条可显示的 Dialogue 成为一个分段（`timing_source=human`、`source_ranges=[]`、`visual_ids=[]`、`role=unknown`、标记 `human_authored`、新的 cue id），按开始时间排序；注释行、绘图事件、无可见文字的事件列在 `omitted_events` 里不进入翻译。旧分段的字符范围与词时钟**不复制**到新分段：与基础源版本的 exact 关系只作为 `correspondences.json` 里的一行参考存在。源 ASS 的样式原字节保留在源版本里，不当成中文成品样式。边界容差按源版本的毫秒值取 1 ms。
- **确认**（3.3、6.6）：`reviews/attestations/<attestation_id>.json`，只写一次；状态由"版本 id + 文件哈希 + 媒体 id"完全匹配的确认记录现场计算，文件变了旧确认自然不再适用。读不出的确认文件不计入并在提示里列出。自动流水线够不着确认入口：`PipelineIsolationTests` 逐个解析 `app/tools/upgrade/*.py`，只有 `cli.py` 引入 `revisions`，其中只有用户命令 `cmd_review_attest` 引用 `attest_revision`。
- **解析版本**（`resolve_revision`）：必须给出具体 revision_id（没有 `latest`）；返回前按 manifest 逐个核对文件哈希，不符即 `ARTIFACT_CHANGED`；只读版本目录，不需要原视频在场，所以视频被移走后仍能取到已登记的人工稿及其确认状态。
- **质量**（`quality.check_quality`，W05 范围）：`QC01_PARSE`、`QC02_TIME`、`QC03_BOUNDS`（SRT 1 ms / ASS 10 ms，容差内记 INFO 并保留原值）、`QC06_ORDER`（人工文件为 INFO，不重排）、`QC20_REVIEW_BASE`（基础版本已更新 / 来源映射不完整 / 样式差异）、`QC21_MEDIA_BINDING`、`QC23_EMPTY`。其余软规则与 review.csv/html 属 W06。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/revisions.py`（新） | `create_session`、`import_revision`、`attest_revision`、`resolve_revision` 及快照、对应关系、样式差异、人工源分段、候选保留 |
| `app/tools/upgrade/quality.py`（新） | `check_quality(QualityInput)`（W05 范围的规则）、`confirmation`（人工状态计算）、`make_issue` |
| `app/tools/upgrade/subtitles.py` | 严格解码与只读 SRT/ASS 解析（7b146e9）；本次补两处：`\h` 显示为不换行空格、时间检查只针对 Dialogue（Comment 行的 0 时长不算错误） |
| `app/tools/upgrade/contracts.py`、`schemas/` | `CorrespondenceSet`、`ViewCue`、`SubtitleView`、`ResolvedRevision`、`QualityInput`（7b146e9） |
| `app/tools/upgrade/cli.py` | `review create\|import\|attest`、`source import`；参数错误改为 JSON 结果 + 退出码 2 |
| `tests/upgrade_revisions_test.py`（新） | 15 个用例：14 个建立在真实流水线产出的机器版本上，1 个核对导入关系 |

未改动：旧 GUI、旧流水线、`subtitle_axis_align.py`、`subtitle_segments.py`、`stages.py`、`source.py`（人工源版本直接走 W04 已有的 `supplied_source` 路径，无需改动提交调用）、旧 22 个测试。

## 验证

| 项（计划 W05 验收 / T05） | 状态 | 证据 |
| --- | --- | --- |
| SRT 改字 + 改轴 + 拆并 + 重编号可登记 | PASS | `test_an_edited_srt_keeps_its_bytes_and_only_identical_unique_cues_count_as_exact`：登记后的 `original.srt` 与用户文件逐字节相同；只有完全相同且唯一的条目是 exact，文字相同但改了时间的是 candidate（同时间改了字的情形在 ASS 用例里），时间和文字都变了的、拆出来的新条目是 unmapped；编号乱序只记 INFO |
| ASS 自定义字段/样式/标签字节保留 | PASS | `test_an_edited_ass_is_stored_byte_for_byte_with_every_field_style_and_tag`：Aegisub 私有段、注释行、自定义样式、`{\pos…\fad…}` 标签、绘图事件、带逗号的文本全部原样；样式增改列为 QC20 WARNING |
| 缺 sidecar 不猜映射 | PASS | `test_a_file_from_elsewhere_is_registered_unmapped_and_its_encoding_is_never_guessed`：无校对副本、无基础版本的外来文件登记为 `unmapped`、`base_revision_id=null`；UTF-16 按 BOM 识别；Shift-JIS 不给编码即拒绝，给了 `cp932` 才登记且原字节不变 |
| 文件正在写入有确定结果 | PASS | `test_a_file_that_is_still_being_written_…`（复制前后变化、PermissionError 两种）；命令行级 `test_a_file_locked_by_the_editor_…` 用真实的 Windows 字节范围锁：退出码 7、可重试、不登记，解锁后同一命令登记成功 |
| base 过期有确定结果；旧 base 更新后旧人工稿仍可取用 | PASS（到"解析版本 + 建立副本"为止；导出本身属 W07） | `test_newer_machine_versions_leave_a_registered_human_file_where_and_as_it_is`：重新识别、换词典之后人工版本目录的文件哈希全部不变，`resolve_revision` 带 `BASE_SUPERSEDED` 提示，仍能据它建立校对副本并登记后续人工稿 |
| 重新识别/翻译/换词典后已提交人工文件 hash 不变 | PASS | 同上（每一步后比较整个版本目录的哈希表与确认记录） |
| 原 media 不符有确定结果 | PASS | `test_a_file_belongs_to_one_media_version_and_a_changed_or_missing_video_has_one_answer`：另一媒体的副本/基础版本 → `MEDIA_CHANGED`；未知媒体 → `INPUT_MISSING`；视频被改 → `MEDIA_CHANGED`（期望/实测哈希）；视频被删 → `INPUT_MISSING`；原字节写回后恢复正常。每种拒绝之后 `.pending`、项目索引、已登记人工稿都没有变化 |
| 原文改稿生成新 source；原文修改不复用错词时钟 | PASS | `test_a_corrected_source_is_a_new_source_revision_translated_without_recognising_again`：新源版本的分段 `timing_source=human`、`source_ranges` 为空、无词时钟；随后的 supplied_source 任务没有任何识别引擎调用，只翻译并发布新的机器版本，原源版本与原识别证据哈希不变。`test_a_source_ass_feeds_only_its_displayed_text_and_times_…`：源 ASS 只取显示文字/时间，样式原字节留在源版本 |
| 确认范围 | PASS | `test_confirmation_is_explicit_per_scope_and_bound_to_one_file_hash`：PARTIAL → REVIEWED 的各组合、SRT 拒绝 style、空/重复/`all` 被拒、确认绑定文件哈希、机器版本按格式分别确认 |
| 结构错误不提交 | PASS | `test_structural_errors_keep_the_file_as_a_candidate_and_commit_nothing` |
| 校对副本是字节副本 | PASS | `test_a_session_is_a_byte_copy_and_the_committed_file_is_never_the_working_file` |
| 12.1 命令行契约（一份 JSON、退出码 0/2/3/5/7、不接受 `latest` / `--zh-file` / `all-auto`） | PASS | `CommandLineTests` 3 个用例，全部经真实子进程调用 `python -m upgrade.cli` |
| 12.1 自动流水线不能代替人确认 | PASS | `PipelineIsolationTests.test_only_the_users_attest_command_can_reach_attest_revision`（静态核对导入与引用关系） |
| T05：旧任务副本迁移后旧 hash 不变 | NOT_RUN | 迁移属 W08 |
| 旧人工稿的导出 | NOT_RUN | 导出属 W07 |
| 真实编辑器（Subtitle Edit、Aegisub）保存出的文件 | NOT_RUN | 执行机未安装；W09 实测。本包的 ASS/SRT 样本是按这两种编辑器的文件形态手写的 |

全套：旧 22 个 + 新 13 个测试文件，35/35 通过（Windows、CPython 3.10.6 隔离 venv）。证据文件：`LOCAL_WORKSPACE/roast-w05-tests-summary.json`。运行方式不变：

```powershell
python scripts/run_tests.py --ffmpeg <ffmpeg.exe> --ffprobe <ffprobe.exe>
```

`upgrade_revisions_test.py` 的 14 个用例都建立在真实 ffmpeg/ffprobe 跑出来的八阶段流水线结果上（`SessionAndImportTests` 全类共用一次运行，其余每个用例各跑自己的；识别引擎在进程启动处替身、模型服务为回环脚本进程），没有 ffmpeg/ffprobe 时这 14 个显示为 skipped，导入关系用例照常运行。

实施中由测试发现并修正的问题：

1. Comment 行在 Aegisub 里常写成 0 时长，最初被当作 `QC02_TIME` 错误而拒绝整个文件；改为只对 Dialogue 做时间检查。
2. `\h` 最初被当普通字符留在显示文字里，导致阅读文字与编辑器所见不一致；改为不换行空格。
3. 未知媒体 id 最初落到通用的"读不到记录"错误；改为点明"项目里没有这个媒体"并带 `QC21_MEDIA_BINDING`。
4. 参数错误原来由 argparse 直接打印 usage、stdout 没有 JSON，不符合 12.1；改为一份 `OperationResult`。

## 数据保护

未修改任何旧业务文件。测试只写临时目录。被登记的字幕文件只读（真实文件锁用例验证了锁定期间与之后文件字节不变）。没有访问外部网站，没有安装软件，没有写用户文档目录。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 人工稿的导出、ASS↔SRT 派生 | 属 W07 | 版本解析与格式/确认状态已就绪，W07 直接消费 `resolve_revision` |
| 旧任务迁移后的人工副本 | 属 W08 | T05 的迁移用例未运行 |
| 真实编辑器未实测 | 执行机没有 Subtitle Edit / Aegisub | 留 W09；特别是 Aegisub 的 `[Aegisub Extradata]`、Subtitle Edit 的 ANSI 保存选项 |
| 相同文件重复登记不去重 | 计划规定每次登记都是新版本 | 用户重复点击会得到内容相同的多个版本，互不影响 |
| 候选（结构错误的文件）留在 `.pending` | 4.2/9.2 规定保留候选 | 目前没有清理命令；W08 的界面需要列出并允许丢弃 |
| 没有在界面上显示提示 | GUI 属 W08 | `BASE_SUPERSEDED` 等提示目前只在命令返回值的 `warnings` 与 `resolve_revision` 的 `notices` 里 |
| 3.13 下未运行 | 未安装（B02） | 同 W01 |

## 回滚

删除 `revisions.py`、`quality.py`、`tests/upgrade_revisions_test.py`，撤回 `cli.py` 的 W05 片段（四个命令与 `_Parser`）和 `subtitles.py` 的两处小改，即回到 7b146e9；再撤回 7b146e9 即回到 W04（b369044）。v2 DATA_ROOT 里的 `reviews/`、`revisions/review/`、`origin=human` 的源版本可整体删除，不影响机器版本和旧 `app/work` 任务。用户自己的 SRT/ASS 文件从未被改动，可直接继续用外部编辑器。

## 下一包

W06 质量规则与人工定位（依赖 W05 已满足）：补齐 QC07–QC19、QC22 等软规则与阈值配置，生成 `review.csv` / `review.html`。
