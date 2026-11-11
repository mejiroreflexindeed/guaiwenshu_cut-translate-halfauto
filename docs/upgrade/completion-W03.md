# completion-W03

```text
工作包：W03 媒体坐标与两类识别接入；计划版本：1.1；基线提交：1a9a0104…（W02 提交 37e973a）
需求：FR01、FR02、FR03、FR12；决策：D04、D05、D06、D12、D20 相关；验证：T03（识别/时钟夹具部分）、T06（媒体部分）
```

## 实现

用户可观察的新行为（命令行；GUI 接入在 W08）：

| 行为 | 说明 |
| --- | --- |
| `queue run` 现在真的执行前四个阶段 | INPUT（登记/下载/探测）→ MEDIA_PREP（时基与必要派生）→ OCR → ASR，之后在 SOURCE 处以 `STAGE_UNAVAILABLE` 停下（W04 未实施，没有占位结果） |
| 视频被登记为项目媒体 | 完整 SHA-256、ffprobe 原始输出、规范化的流信息、时间原点与时长；原文件不改动；同内容再次提交是同一个媒体 |
| 选流必须明确 | 只有一个真实视频流/一条音轨时自动选；多条时返回 `NEEDS_SELECTION` 并列出候选；`submit --video` 在已登记 ffprobe 时当场检查，不把含糊的请求放进队列 |
| 无音轨不会被悄悄改模式 | combined / speech_only 遇到无音轨媒体报 `NEEDS_SELECTION`（提示改用 visual_only）；visual_only 不提取音频、不启动 Whisper |
| 单视频 URL | 由锁定版本的 yt-dlp 下载到本项目 `media/downloads/`，保留 info JSON 与实际文件身份，固定 `--no-playlist` |
| 缺工具/模型时先说清楚 | INPUT 阶段一次点名本请求需要的全部缺失角色（`RESOURCE_MISSING`），不探测、不派生、不启动引擎；补登记后 `resume` 继续同一个 run |
| 不支持或无法确定时间的媒体有明确结果 | `MEDIA_CLOCK_UNKNOWN`、`MEDIA_CLOCK_DISCONTINUITY`、`MEDIA_PROFILE_UNSUPPORTED`；媒体仍被登记，原文件保留 |
| 只重算有依赖的阶段 | 换时间轴模式或识别模式复用已有 OCR/ASR 证据；换 OCR 区域只重跑 OCR；换音轨只重做音频派生与 ASR |

机制（计划第 7、8 节）：

- **唯一时间轴**：t=0 是所选视频流第一个实际呈现帧；`t_ms = round_half_up(1000 × (pts × time_base − 原点秒))`，全程整数/Fraction，不用 float。原点、片尾、帧间隔来自包级时间戳，不由后缀或平均帧率推断，未知的 start_time 不当作 0。
- **派生媒体**（`media/<media_id>/derived/<media_revision>/`，一次提交、带 manifest 与 `derivation.json`，按内容+参数+工具身份签名复用）：
  - 常规 CFR、起点 0、无旋转的 H.264 MP4 直接使用原文件；其余生成同一时钟的 H.264 复核/识别视频，并逐帧核对源与派生的毫秒时间后才允许使用。
  - ASR WAV 为 16 kHz 单声道 s16le，样本 0 即视频时间 0：音轨起点相对视频原点的偏移用实测值，早的裁掉、晚的补静音、短于视频的尾部补零；无音轨不生成。
- **两类识别**（`recognize(RecognitionRequest)`，引擎与权重不变）：
  - 语音：沿用旧 whisper.cpp 命令行、字节级 token 重组、VAD 时钟冲突时"同音频同模型去掉 VAD 重跑一次、两次都留证"的恢复；输出 SpeechEvidence。
  - 画面文字：沿用旧 MagiaTimeline 的逐任务 ASCII 配置、UTF-8 兼容副本、`dialogRect` 顺序与无损解码；输出 VisualEvidence（当前引擎只给区间与文字，固定 `interval_only`，分数为 null）。
  - 原始引擎文件与规范化证据一起写入阶段 attempt 并计入 manifest；空结果是带提示的合法结果（`ASR_EMPTY`、`OCR_EMPTY`），失败的输出只作候选保留。
- **进程**：ffprobe、ffmpeg、yt-dlp、两个识别引擎全部经 W02 的自有进程执行器启动（argv、无 shell、可取消）。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/media.py`（新） | `probe_media`、`prepare_media`、时钟计算、包时间线分析、选流、媒体登记与绑定、两种派生及其校验、`download_media` |
| `app/tools/upgrade/recognition.py`（新） | `recognize`、Whisper 与 Magia 适配、两类证据 |
| `app/tools/upgrade/stages.py`（新） | INPUT / MEDIA_PREP / OCR / ASR 四个阶段实现、角色到资源的解析、提交前选流预检 |
| `app/tools/upgrade/stage_registry.py` | 注册上述四个阶段 |
| `app/tools/upgrade/contracts.py` | 新增记录 `Derivation`、`MediaManifest`、`MediaInput`、`MediaPlan`、`RecognitionRequest`（共 55 种） |
| `app/tools/upgrade/runner.py` | 阶段签名不再包含整个请求指纹（IF-16）；StageContext 增加 `attempt_id` 与每请求的 `scratch` |
| `app/tools/upgrade/process_runner.py` | `ProcessHandle.send_stdin`（Magia 启动后需要一次回车） |
| `app/tools/upgrade/config.py` | 旧 `download_settings` 与 `recognition.dtw_model` 的迁移 |
| `app/tools/upgrade/cli.py` | `submit` 选流预检、内部 `worker` 子命令 |
| `schemas/` | 5 个新 schema，`index.json` 更新 |
| `scripts/run_tests.py` | `--ffprobe` / `FFPROBE_BINARY` |
| `tests/upgrade_media_test.py`、`tests/upgrade_recognition_test.py`、`tests/upgrade_stages_test.py`、`tests/support/`（新） | 50 个用例与夹具 |
| `tests/upgrade_contracts_test.py`、`tests/upgrade_storage_config_test.py` | 导入隔离覆盖新模块；迁移用例覆盖下载设置 |

未改动：旧 GUI、`roast_pipeline.py`、`voicetransl_adapter.py`、`magia_timeline_adapter.py`、`magia_utf8_runtime.py`、分段/对轴算法、旧 22 个测试。新适配器只导入旧模块里的纯函数（SRT/ASS 解析、token 重组、字幕校验、输出解码、UTF-8 副本）。

`media.py` 的第三个 Interface `export_media(ExportRequest)` 属于 W07 的独立出口，本包没有定义它，也没有放占位函数。计划 8.2 的"导出边界参考帧"属于 W06（按需、由用户选择事件），本包未做。

## 验证

| 项（计划 W03 验收） | 状态 | 证据 |
| --- | --- | --- |
| T03 中识别/时钟 fixture | PASS | `upgrade_media_test.py` 时钟 11 用例、`upgrade_recognition_test.py` 16 用例；台账 `T03-W03` |
| T06 媒体小素材 | PASS | `upgrade_media_test.py` 真实 ffmpeg/ffprobe 13 用例；台账 `T06-W03` |
| 四种模式每条阶段 skip 规则准确 | PASS | `test_each_mode_skips_exactly_what_it_does_not_need`、`test_combined_runs_media_then_ocr_then_asr_and_stops_honestly_at_source`（真实媒体阶段 + 引擎替身，校验状态、原因与引擎启动次数） |
| 没有音轨时不造 WAV | PASS | `test_media_without_audio_gets_no_fabricated_wav`、visual_only / supplied_source 用例检查项目内没有任何 `.wav` |
| raw 与 normalized 证据都在 | PASS | 两类证据的 `raw_refs` 指向同一 attempt 内的原始 SRT/JSON/ASS/配置/日志并经哈希校验；探测原始 JSON 与 `media.json` 同在登记 manifest 内 |
| 非零 / VFR / 旋转时钟有确定记录 | PASS | `test_non_zero_start_keeps_one_origin_for_video_and_audio`（origin_pts=1500、提示音落点 500/1500 ms）、`test_variable_frame_timing_is_preserved_frame_by_frame`（逐帧一致）、`test_rotation_is_baked_in_once` |
| 显式选流 | PASS | `test_several_audio_streams_need_an_explicit_choice`、`test_stream_selection_table`、CLI 用例（退出码 2） |
| ASR_EMPTY、OCR 分数 unknown 表达 | PASS（适配协议层） | `test_empty_speech_is_a_warning_…`、`test_missing_scores_are_null_and_reported_once`、`test_engine_intervals_become_visual_evidence_without_invented_scores` |
| FR01 单视频 URL | PASS（回环 HTTP） | `test_single_video_url_is_downloaded_by_yt_dlp_…`；台账 `FR01-URL-W03` |
| FR02 切换只重算有依赖的阶段 | PASS | `test_only_real_dependencies_invalidate_recognition` |
| FR12 缺资源指出缺项、不启动 | PASS | `test_missing_engines_are_named_before_anything_long_starts_and_resume_continues`、`test_changed_resource_content_is_refused` |
| 真实 Whisper / MagiaTimeline 运行 | NOT_RUN | 执行机没有这两个引擎（B01）；计划规定真实引擎整体验证在 W09 一次完成 |

全套：旧 22 个 + 新 8 个测试文件，30/30 通过（Windows、CPython 3.10.6 隔离 venv）。证据文件：`LOCAL_WORKSPACE/roast-w03-tests-summary.json`。运行方式：

```powershell
python scripts/run_tests.py --ffmpeg <ffmpeg.exe> --ffprobe <ffprobe.exe>
```

没有 ffprobe（或 ffmpeg 没有 libx264）时，`upgrade_media_test.py` 的 13 个和 `upgrade_stages_test.py` 的 10 个真实媒体用例显示为 skipped，不算通过。

实施中由校验步骤发现并修正的两个真实问题（已写入 IF-14）：

1. 用 `setpts` 平移原点时，MP4 派生文件的最后一帧被 edit list 裁掉（帧数 90→89）。改为在已编码的包上用 `setts` 平移，帧时长得以保留。
2. 不指定编码时间基时，VFR 的时间被吸附到 1/帧率网格（最大偏差 11 ms）。改为显式使用源流时间基后逐帧一致。

这两个问题在只看 FFmpeg 退出码的做法下都不会被发现。

## 数据保护

未修改任何旧业务文件。测试只写临时目录；素材由测试现场生成。对执行机外部工具目录只读（运行其中的 ffmpeg/ffprobe，未复制、未改动）。原媒体在全部用例前后哈希相同。没有访问任何外部网站（URL 用例只连回环地址）。未写用户文档目录，未安装任何软件。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 真实 Whisper / Magia 的调用未实测 | 执行机没有引擎与模型（B01） | 命令行与解析按旧适配器逐项移植并用替身验证；真实输出形态的三个假设见 `decisions.md` Q-W03-2，留 W09 |
| SOURCE 及之后的阶段 | 属 W04/W06 | `queue run` 对任何请求最终以 `STAGE_UNAVAILABLE` 结束，还不能产出字幕 |
| supplied_source 从已登记的源字幕版本取媒体 | 源字幕版本（`source import`）属 W05 | 目前 supplied_source 需要随请求给出视频文件，否则 INPUT 报 `INPUT_MISSING` |
| 末帧无时长的 VFR MKV | 规则下为 `MEDIA_CLOCK_UNKNOWN`；是否另行规定需负责人决定（Q-W03-1） | 这类文件目前不能进入识别 |
| ffmpeg 与 ffprobe 不是同一发布版本，也不是计划引用的 n7.1 | 执行机现有资源如此 | T06-W03 的结论限于这一对工具；正式来源确定后重跑受影响用例 |
| 音频只校验起点与总长 | 未检查音轨中途的时间戳不连续 | 这类素材的 ASR 时间可能在断点后偏移；证据的 coverage_status 仍是 `process_completed_only` |
| 已提交的 MEDIA_PREP 被复用时不重新检查派生文件是否还在 | 派生目录在阶段 attempt 之外 | 手工删除派生目录后，后续阶段会以 `INPUT_MISSING` 失败而不是自动重建 |
| 3.13 下未运行 | 未安装（B02） | 同 W01 |
| CLI/契约补充待确认 | CO-CAND-003 | 文档 |

## 回滚

删除 `media.py`、`recognition.py`、`stages.py`、三个新测试文件与 `tests/support/`、五个新 schema，并撤回 `stage_registry.py`、`contracts.py`、`runner.py`、`process_runner.py`、`config.py`、`cli.py`、`scripts/run_tests.py` 与两个既有测试的 W03 片段，即回到 W02（37e973a）。旧引擎、模型与源媒体未被改动；v2 DATA_ROOT 里的 `media/` 派生目录可以整体删除，不影响旧 `app/work` 任务。旧三种模式仍可在旧入口使用。

## 下一包

W04 主记录、词典、翻译和机器字幕（依赖 W03 已满足）：把 SpeechEvidence / VisualEvidence 接到 v8 分段，产出 SourceRevision、译文与机器 SRT/ASS。翻译的真实运行需要 llama-server 与 Sakura-GalTransl-7B-v3.7（执行机没有，B01），协议部分可用回环 HTTP 验证（T04）。
