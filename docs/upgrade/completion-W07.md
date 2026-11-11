# completion-W07

```text
工作包：W07 独立字幕/视频出口；计划版本：1.1；基线提交：1a9a0104…（W06 提交 2f79e47）
需求：FR10、FR11；决策：D02、D13、D14、D16 相关；验证：T06（导出部分，整组随本包结算）
里程碑：M2（受保护的人工字幕和显式成片闭环）——命令行层面达成，GUI 入口属 W08
```

## 实现

用户可观察的新行为（命令行；GUI 按钮“导出字幕”“导出视频”在 W08 接入）：

| 行为 | 说明 |
| --- | --- |
| `export --kind subtitles` | 把选定版本的字幕**原字节**复制到一个新目录（`--destination`，已存在即 `OUTPUT_EXISTS`）。机器版本给出 SRT 和 ASS 两个文件（`--format` 只要其一）；人工版本只给它自己的格式，文件名沿用登记时的文件名。附 `manifest.json`（ExportManifest）与 `quality.json` 快照；`--include-media` 可同时复制原视频（按登记哈希核对） |
| 显式格式转换 | `--convert-to srt`：从 ASS 的显示文字生成 `<名>.derived.srt`（保留时间与换行、去掉标签），标 `lossy=true`、`UNREVIEWED`，原 ASS 一并导出；有绘图事件时必须再加 `--accept-omitted-drawings`，否则 `NEEDS_SELECTION` 并列出会被省略的事件。`--convert-to ass`：人工 SRT 用第 7 节固定基础样式生成派生 ASS。人工确认不继承，只在 manifest 里引用原确认 |
| `export --kind mp4_burn` | `mp4_burn_sdr_v1`：libx264 CRF18 medium、yuv420p、选定音轨 AAC 192k（`--audio-stream none` 则没有音轨）、faststart、不附软字幕。ASS 把**完整的已选文件**交给 libass（不 force_style）；SRT 先生成派生渲染用 ASS 并另存。奇数宽高只在右/下补 1 像素黑边并写进 manifest。HDR/杜比视界/立体/多节目源拒绝（`MEDIA_PROFILE_UNSUPPORTED`），字幕文件仍可单独导出 |
| `export --kind mkv_soft` | `mkv_soft_v1`：选定视频/音频流原样复制（不重编码），附选定 SRT 或 ASS 作为字幕轨：默认轨=是、强制=否、语言 `chi`；源文件里的其他字幕与附件不复制；不附字体 |
| 不启动模型 | 导出只用 ffmpeg/ffprobe，不调用 Whisper/Magia/Sakura，也不调用旧流水线 |
| 提示不拦截，状态跟着文件走 | 结构错误的版本不能导出；软提示只写进 manifest 与返回值（每个文件的 `human_status` / `confirmed_scopes`，未完整确认的文件给出 `NOT_REVIEWED` 提示） |
| 字体 | 导出视频需要的字体（样式表 Fontname 与行内 `\fn`）或字形缺失 → `FONT_MISSING`（退出码 3），不生成任何文件，不静默换字体；只导出字幕时缺字体只是 QC22 WARNING |
| 媒体身份 | `--media` 指定的媒体与字幕绑定的媒体不同 → `MEDIA_CHANGED`（`rule_id=QC21_MEDIA_BINDING`），不按同名文件认可 |
| 失败/取消不发布 | 输出先写在目标目录旁的 `.<名>.pending-<id>/`，校验通过后整体改名；失败或取消时目标目录不出现，候选目录保留并带 `candidate.json`（`NOT_COMMITTED`）供诊断 |
| 项目记录 | 项目里 `exports/<export_id>/location.json` 记下导出位置与 manifest 哈希，`project.json` 的 `export_ids` 增加一项 |

机制（计划第 7.5、9.5、12.1 节）：

- **ASCII 工作目录**：滤镜字符串里只出现固定名字 `input.ass` 和 `fonts`，工作目录在 `ascii_temp_root/export/<id>/` 下，原 ASS 按字节复制进去，需要的字体文件复制到 `fonts/`；媒体路径与输出路径都作为独立的命令行参数，从不拼进滤镜字符串。导出结束即删除工作目录。
- **时钟**：所有字幕时间的零点是"第一帧被显示的视频帧"。视频导出一律用 `-copyts`，滤镜和封装器看到的是源时间戳本身；起点不为 0 的源，硬字幕在 `subtitles` 滤镜前后用 `setpts` 平移，软字幕用 `-itsoffset` 平移字幕输入。实施中发现：不用 `-copyts` 时 MKV 封装器会因 AAC 预滚把所有流整体后移约 21 ms，按文件起点推算偏移会把字幕轨放错 21 ms（见下文“由测试发现的问题”）。
- **校验**（7.5）：ffmpeg 退出 0；ffprobe 能读；流数量与编码符合 profile（硬字幕 h264/yuv420p + aac、无字幕流；软字幕视频/音频编码与源相同、字幕轨编码/默认/强制/语言正确）；输出视频时长与源相差不超过 `max(最大帧间隔, 50 ms)`；软字幕再用 `-copyts` 把字幕轨提取出来，与输入逐条比对时间和文字（相对输出的第一帧视频；ASS 容差 10 ms，SRT 1 ms）。任何一项不符即 `EXPORT_VALIDATION_FAILED`，不截尾凑数。
- **字体检查**：只看 `--font-dir` 给出的目录和 Windows 系统/用户字体目录；按字体文件 `name` 表（家族名、全名、PostScript 名）匹配，按 `cmap`（格式 4/12）检查每种字体要显示的字符是否都有字形。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/export.py`（新） | 字体目录扫描与字形检查、三个固定 profile、字幕导出与格式转换、硬字幕/软字幕导出、输出校验、一次性发布与项目记录 |
| `app/tools/upgrade/quality.py` | `export_issues()`：导出时的 QC21/QC22 |
| `app/tools/upgrade/cli.py` | `export` 命令 |
| `tests/upgrade_export_test.py`（新） | 7 个用例（见下） |

未改动：旧 GUI、旧流水线（含剪映流程，D16）、`contracts.py` 与 `schemas/`（ExportRequest/ExportManifest 在 W01 已定义）、旧 22 个测试。

## 验证

| 项（计划 W07 验收 / T06 导出部分） | 状态 | 证据 |
| --- | --- | --- |
| 无模型进程也能从已选人工 ASS 渲染 | PASS | `test_a_burned_mp4_shows_the_complete_human_ass_on_the_real_frames`：导出前后引擎替身调用次数、模型请求数不变 |
| 文字/位置/样式来自完整人工文件（S06：2 个样式、2 层、行内位置与字体、换行、正文逗号、Comment） | PASS | 同上：manifest 记 `render_input=complete_ass_bytes`，导出目录里的输入副本与人工文件逐字节相同；成片在字幕时段与原片逐帧（按帧序号）比较差异显著、字幕结束后无差异 |
| MP4 字幕实帧 | PASS | 同上；`SourceClockTests` 对起点 1.4 s（音频 0.6 s）的 MKV 与可变帧率 MP4 分别检查规范时间 0.3 s（未出现）、1.0 s（出现）、2.0 s（消失） |
| 音轨 / 无音轨 | PASS | 有音轨为 aac；`--audio-stream none` 只有视频流 |
| MKV 字幕轨及提取对照 | PASS | `test_a_soft_mkv_carries_the_subtitle_track_that_can_be_extracted_again`：ASS 轨（默认/非强制/chi），提取后 3 条全部一致；机器 SRT 轨为 subrip，提取一致；视频/音频编码与源相同（未重编码）；起点 1.4 s 的源同样一致 |
| VFR / offset / rotation 时钟；奇数尺寸 | PASS | `SourceClockTests`：可变帧率与偏移起点见上；奇数尺寸 321×241 输出 322×242 并记 `padded_to_even`；旋转 90° 的源输出 240×320 且不再带旋转元数据（只转一次） |
| 缺字体拒绝 | PASS | `test_missing_fonts_only_warn_for_subtitle_files`：成片 `FONT_MISSING`、不建目录；字幕导出只给 QC22 WARNING |
| 取消/失败不发布 | PASS | `test_a_cancelled_or_refused_export_publishes_nothing`：取消后目标目录不存在、候选带 `NOT_COMMITTED`、项目 `export_ids` 不变；另一个登记媒体 → `MEDIA_CHANGED`（QC21） |
| 已存在输出不覆盖 | PASS | `test_a_subtitle_export_copies_the_chosen_bytes_once_…`：同一目标再次导出 → `OUTPUT_EXISTS`，目录内容与索引都不变 |
| 字幕导出原字节、确认状态随文件 | PASS | 同上；人工 ASS 只导出原格式；ASS→SRT 派生为 lossy、UNREVIEWED，绘图事件需明确确认 |
| 旧成片/源视频不变 | PASS | 导出前后源视频哈希相同；旧导出目录在后续导出中不被触碰 |
| 真实影视素材、真实编辑器往返（Aegisub 保存后渲染比对） | NOT_RUN | T08/T09 属 W09（B05）；本包素材是测试代码生成的合成画面 |
| 计划引用的 FFmpeg n7.1 构建 | NOT_RUN | 执行机是 2024-09 的 gyan.dev git 构建（含 libass/libx264）；同 T06-W03 |

全套：旧 22 个 + 新 15 个测试文件，37/37 通过（Windows、CPython 3.10.6 隔离 venv，2026-11-10T13:46Z UTC）。证据文件：`LOCAL_WORKSPACE/roast-w07-tests-summary.json`。

实施中由测试发现并修正的问题：

1. 软字幕最初按"首帧时间 − 文件起点"推算偏移；源 MP4 的 AAC 预滚让文件起点为 −21 ms，MKV 封装器又把所有流整体后移，结果字幕轨相对视频晚了约 21 ms，被提取比对抓到。改为 `-copyts` + 以首帧时间为唯一偏移。
2. `--include-media` 最初读错了开关位置，视频没有被复制；测试抓到后修正。
3. 硬字幕的像素比对最初按秒寻址，两个文件的容器起点不同导致比较的不是同一帧；测试改为按帧序号取帧。

## 数据保护

没有修改旧业务文件。原视频与已登记的字幕只读。导出只写用户指定的新目录、ASCII 临时工作目录（结束即删）和项目日志/索引。测试只写临时目录。没有访问外部网站，没有安装软件或字体。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 导出文件放在用户指定目录，项目里只记位置 | 计划 4.2 写的是项目内 `exports/<export_id>/`，但成片放两份太占空间（CO-CAND-007） | 需负责人确认；改成项目内只需改 `_record` 与目标路径 |
| MKV 不附字体 | 没有"允许分发的字体清单"（B07） | 播放端需要自行安装字体 |
| 编码进度只写日志 | 界面进度条属 W08 | 命令行导出期间没有百分比显示 |
| 内嵌 `\fn` 的字形检查按整条字幕文字计算 | 不逐个解析覆盖标签的作用范围 | 可能把本不需要的字也算给行内字体，只会多报、不会漏报 |
| 硬件编码、其他 profile | 计划规定需独立 profile 与新验证 | 未实现 |
| 3.13 下未运行 | B02 | 同前 |

## 回滚

撤回本提交即回到 W06（2f79e47）：删除 `export.py`、`tests/upgrade_export_test.py`，撤回 `cli.py` 的 `export` 命令与 `quality.export_issues`。已导出的目录是独立文件，可以保留或删除；项目里的 `exports/` 与 `export_ids` 只是记录。按计划，禁用视频出口后字幕仍可独立交付，并可在 Subtitle Edit 等外部工具里自行成片。

## 下一包

W08 GUI 整合和旧项目迁移（依赖 W07 已满足）：在现有 tkinter 界面里接入"查看复核报告 / 打开输出目录 / 导出字幕 / 导出视频 / 标记人工确认"，以及 `roast migrate` 旧任务迁移。
