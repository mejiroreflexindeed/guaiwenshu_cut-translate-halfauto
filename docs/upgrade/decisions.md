# 升级实施决策台账（W00 冻结）

计划依据：`docs/architecture-audit/08-complete-upgrade-plan.md` 版本 1.1（2026-10-25）。本文件记录 W00 冻结的基线事实、原样采纳的决策、执行环境与计划假设的差异及其处理方式。变更单编号沿用计划第 0.2 节格式；W00 **没有**提出任何变更单。

## 1. 冻结基线

| 项 | 值 |
| --- | --- |
| 基线提交 | `1a9a0104defbd9881db3f9eded017ad7b1f96edc`（2026-10-28，仅新增审计文档） |
| 业务代码提交 | `93df2625f5bfa71e8dd9bb897beac3d01a730b10`（2026-10-22）；1a9a010 没有改动 app/tests/scripts/build |
| 升级分支 | `upgrade/subtitle-workflow-v2`，从 1a9a010 建立 |
| 发布清单核对 | `docs/source_manifest.json` 51 条 `published_sha256` 与当前文件逐一相同（0 不符、0 缺失）。清单中的 `source_sha256`（fix_stage 侧）只能按清单采信，执行机上没有 fix_stage |
| 旧测试基线 | 22/22 通过（隔离 venv，声明依赖 + PyYAML 6.0.3）；未装 PyYAML 时 12 个失败，见 `baseline.json` |
| 旧准确性结论登记 | 按审计报告 01：`accuracy_accepted=false`、`native_ui_accepted=false`；35 秒 mixed/speech 各 15 条、13 条待复核、3 处术语未遵循；零源字符遗漏只是结构覆盖证明。**本轮未重跑，也无法在执行机上重跑**（素材不在本机） |

## 2. 原样采纳的决策

D01–D20、FR01–FR16、QC01–QC23、W00–W10、T01–T09、B01–B11 全部按计划 1.1 采纳，编号索引见 `traceability.json`。特别重申与执行机环境直接相关的几条：

- **D06/D20**：默认引擎仍是 whisper.cpp GGML large-v3 + VAD、MagiaTimeline、Sakura-GalTransl-7B-v3.7 via llama-server。执行机上存在的 faster-whisper（CTranslate2）、Sakura-13B-LNovel-v0.9b 和 2024-02 版 llama.cpp `server.exe` **不是**这些资源，W00 只登记，不替换、不作为新后端。
- **D12/D18**：资源外置、仓库不捆绑二进制/模型；执行机资源只以逻辑 resource_id + 本机私有路径绑定登记。
- **第 12.2 节**：主运行时目标 CPython 3.13；执行机只有 3.10.6，见第 3 节 B02 处理。

## 3. 执行环境与计划假设的差异

计划的事实基线来自维护者本机（`LOCAL_WORKSPACE`、`LEGACY_ROOT`、`EXTERNAL_TOOLS_ROOT`）。W00 由另一台机器上的测试侧账号执行，差异与处理如下：

| 编号 | 差异 | 处理 | 影响范围 |
| --- | --- | --- | --- |
| ENV-01 | 执行机没有 `fix_stage`、`implementation_20261010`、`candidate_20261011`、`LEGACY_ROOT` | W00 任务 1 的"比较 fix_stage AST"改为核对 `source_manifest.json` 的 51 条发布哈希（全部一致）；fix_stage 侧哈希按清单采信并标 `reused_from` | 不阻塞 W01–W08；W09 需要维护者机器或其提供素材 |
| ENV-02 | S01/S02/S04/S07 素材不在执行机 | 只登记计划中的路径别名与 manifest hash；T07/T08 的真实素材验证标 BLOCKED（B08/B09 相关） | W09 |
| ENV-03 | 执行机没有 whisper-cli/ggml-large-v3/VAD、MagiaTimeline、llama-server、Sakura-GalTransl-7B-v3.7、yt-dlp、ffprobe、Subtitle Edit、Aegisub（**W03 更正**：ffprobe 实际存在，见 5c 的 ENV-08；yt-dlp 以锁定的 Python 包形式存在于开发环境） | 在 `resources-inventory.json` 中以 `present=false` 登记为必需资源缺失；按 B01/B05 处理，不自动下载、不换后端 | W03 真实引擎调用、W07 ffprobe、W09 |
| ENV-04 | 执行机只有 CPython 3.10.6（计划记录维护者为 3.10.11），没有 3.13 | 3.10 venv 仅用于旧测试基线复现；B02 按第 12.2 节在 W01 确定 3.13 版本并单独申请安装授权 | W01 冻结包 |
| ENV-05 | 执行账号对上游仓库只有读权限 | 提交留在本地分支；推送与 PR 需负责人授权（写权限或接受分叉 PR） | 交付方式 |
| ENV-06 | `upgrade_stage` 目录名 | 隔离 checkout 实际位于 `LOCAL_WORKSPACE/repo-roast`，作用等同计划第 4.1 节的 `upgrade_stage/`；开发用 DATA_ROOT 将建在仓库外 `LOCAL_WORKSPACE/roast-data-dev`，不写用户文档目录 | 无 |
| ENV-07 | 执行机 GPU 为 NVIDIA RTX 4080 SUPER 16 GiB（驱动 617.42） | 只登记；维护者历史证据为 Intel Iris Xe/Vulkan。B04 的 profile 实测仍留 W09，不能用执行机结果替代维护者机器 | W09 |

## 4. W00 期间确认的客观事实

- **PyYAML 缺口属实**（审计 P1）：`voice_dictionary.py`、`magia_timeline_adapter.py` 无条件 `import yaml`，`requirements.txt` 未列；干净 3.10 环境下 10 个测试因 `No module named 'yaml'` 失败、1 个因 `pywinauto`（legacy 组）失败、1 个因前者连带失败。W01 按计划显式声明 PyYAML 6.0.3。
- **shell=True 命令模板属实**（审计 P1）：`roast_pipeline.run_command` 以 `shell=True` 执行格式化后的字符串；W02 按第 6.3/10.5 节改 argv。
- **词典待确认条目**（B06）：`app/config/dict_gpt.txt` 中 `ラモーヌ`、`ラモさん` 两条标 `[待确认]`；`translation_glossary.json` 的 `アルトレ` 标"根据上下文选择"。W00 如实登记，不替用户决定译名。
- **资源来源**（B01）：执行机上的 faster-whisper 与 Sakura 13B 来自 GalTransl-ASMR 20241110 整合包，均为第三方整合分发；它们对 D06 默认工作流无直接作用，仅作本机资源登记。

## 5. 负责人需要提供或决定的事项（不是变更单）

| 项 | 需要什么 | 对应阻塞 |
| --- | --- | --- |
| 许可证 | 回答 `license-options.md` 第 5 节三个问题 | B10 |
| 默认引擎资源 | 提供或授权获取 whisper.cpp（whisper-cli + ggml-large-v3 + silero VAD）、MagiaTimeline、llama-server + Sakura-GalTransl-7B-v3.7、ffprobe、yt-dlp 的官方来源；或在维护者机器上运行 W03/W09 | B01 |
| Python 3.13 | 授权安装 W01 选定的 3.13 补丁版 | B02 |
| 验收素材 | S01/S02 原片或在维护者机器上执行 W09 | B08 |
| 编辑器 | 授权安装 Subtitle Edit 与 Aegisub 指定版本 | B05 |
| 干净测试机 | 提供 | B09 |

## 5a. W01 记录（2026-11-08）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| RT-01 | 主运行时选定 **CPython 3.13.17**（2026-10-21，最后一个带 Windows x64 安装包的 3.13 版本；3.13.18 起只有源码）。安装包 URL、python.org 公布的 SHA-256、Sigstore/签名地址记录在 `runtime-lock.json`；**尚未下载安装**，需负责人授权（B02） | 计划 12.2 选版规则；python.org 发布页 |
| RT-02 | 依赖分三组锁：核心（PyYAML 6.0.3、yt-dlp 2026.7.4）、legacy（pywinauto、pywin32、Pillow）、dev（imageio-ffmpeg）；`requirements.txt` 仍等于全量安装以兼容旧文档。3.13 带哈希的 transitive lock 因无 3.13 解释器 BLOCKED；3.10 开发 venv 的 freeze 记录在 `requirements-lock-dev-3.10.txt` | 计划 12.2 |
| IF-01 | `app/tools/upgrade/` 新增 contracts / config / storage / resource_catalog / cli；记录类型全部导出到 `schemas/`（`scripts/export_schemas.py --check` 可验证一致）。**W03 更正**：W01 实际是 50 种记录类型（50 个 schema 文件 + index），原文"39 种 + 资源声明文件 3 种"和 completion-W01 的"43 文件"是笔误，代码与导出文件一直是 50；W03 后为 55 | 计划第 5、6 节 |
| IF-02 | 引擎参数（翻译/识别/OCR/媒体/下载）保存在 `settings/engine.json`，与 `settings/app.json`（AppConfig）分开；两者一起进入 ConfigSnapshot | AppConfig 契约字段固定，ConfigSnapshot 单列 `engine_settings` |
| IF-03 | 资源声明文件 `settings/resources.local.json`：`resource_id → {kind, absolute_path, files, expected_content_sha256, …}`。首次 `register` 冻结内容身份；路径变化且内容相同只改绑定；内容变化报 RESOURCE_CHANGED 且 resolve 拒绝 | 计划 4.2、10.3 |
| IF-04 | 能力探针只执行版本/帮助参数（ffmpeg `-version`、whisper `--help`、llama-server `--version`），未探到的能力保持 `unknown`，不适用为 `not_applicable` | 计划 6.2、W01 任务 |
| CO-CAND-001 | CLI 比第 12.1 节清单多了 `config init`、`config migrate`、`resources register`、`resources rebind` 四条命令。它们是第 3.4/4.2/9.6 节已规定行为（首次选数据根、旧配置迁移、资源登记/重新定位）的命令行表达，不新增产品行为；按 0.2 节格式登记为变更单候选，请负责人确认是否纳入 12.1 清单 | 计划 0.2、12.1 |
| EV-01 | 旧 22 测试 + 新 3 个 T01 测试在 3.10 venv 全部通过；3 个 T01 测试在**未装任何第三方包**的系统 3.10 下也通过（v2 核心不依赖 PyYAML/pywin32） | `validation-ledger.json` T01 |

## 5b. W02 记录（2026-11-08）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-05 | 错误码表新增 `STAGE_FAILED`（阶段实现抛出异常）与 `STAGE_UNAVAILABLE`（本构建没有该阶段实现）。10.4 的集合是"最低集合"，新增两码只用于诚实标注实施进度，不改变已有码含义 | 计划 10.4 |
| IF-06 | 阶段实现通过 `upgrade/stage_registry.py` 注册；W02 的默认注册表为空，真实请求在 INPUT 处以 STAGE_UNAVAILABLE 失败并可 resume。不放占位实现、不模拟成功 | 计划 0.1"范围外不以后续完善包装成已完成" |
| IF-07 | 取消正在运行的请求用 `queue/cancel/<request_id>` 标记文件通知执行器（GUI 只写文件、不碰 PID）；处理后删除标记并在 envelope.last_error 记 PROCESS_CANCELLED | 计划 10.2/10.5"GUI 不直接控制外部任意 PID" |
| IF-08 | task.json 中指向阶段 manifest 的 ArtifactRef 路径以**项目目录**为基准（`runs/<run>/stages/<stage>/<attempt>/manifest.json`），这样同一项目内跨 run 复用已提交阶段不需要 `..` | 计划 4.3 托管路径规则 + 10.3 复用 |
| IF-09 | 子进程优雅退出请求在 Windows 用 CTRL_BREAK；无控制台的引擎收不到时，3 秒宽限后由 Job Object（KILL_ON_JOB_CLOSE）终止整个所有权范围；没有 Job Object 时退回旧 `terminate_process_tree`（Toolhelp 快照），绝不按进程名杀 | 计划 10.5 |
| CO-CAND-002 | CLI 再增 `inbox intake --config --inbox`（旧收件箱接管入口）；`config init` 的 `--config` 改为可选，默认写 `<data-root>/settings/app.json`，使 `queue run --data-root` 等 12.1 命令能按 4.2 布局找到配置 | 计划 10.2 inbox 兼容、12.1 |
| EV-02 | T02 20 用例 PASS；全套 27/27（3.10 venv） | `validation-ledger.json` T02 |

## 5c. W03 记录（2026-11-10）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| ENV-08 | 重新清点发现执行机有 ffprobe：`EXTERNAL_TOOLS_ROOT/GPT-SoVITS/runtime/ffprobe.exe`（2022-04-07 gyan full）。W00 登记为缺失是清点范围不足。W03 把它和已登记的 ffmpeg（2024-09-26 gyan essentials）作为显式登记的资源只读使用来跑 T06；两者不是同一发布版本，如实记录。同目录还有 ffmpeg n4.3.2（无 libx264、早于 5.1），只登记不使用。B01 里 ffprobe 一项缩小为"负责人选定交付用的正式 ffmpeg/ffprobe 来源" | `resources-inventory.json` |
| IF-10 | 项目内媒体布局：`media/<media_id>/{media.json, original.ref.json, probe/, source/, derived/<media_revision>/}`，下载在 `media/downloads/<download_id>/`。计划 4.2 写的是单个 `media/media.json`；因为 ProjectRecord.media_ids 是列表、7.4 规定剪辑后的文件是新 media_id，这里按 media_id 分目录。`media.json` 与探测原始输出一次提交（MediaManifest），`original.ref.json` 是可更新的本机绑定（同内容换路径/只改时间戳时刷新） | 计划 4.2、6.8、7.4 |
| IF-11 | media_id 对应"内容 SHA-256 + 所选视频流"：时间轴只由视频流决定。同内容再次登记复用同一 media_id；音轨选择是每个请求自己的（run 内的 MediaRecord 记录本次选择），换音轨不产生新媒体 | 计划 7.2、10.3 |
| IF-12 | 时钟判定规则：原点 = 所选视频流中未被标记 discard 的包的最小 PTS；DTS 在文件顺序中回退、或两个呈现包 PTS 相同 → `MEDIA_CLOCK_DISCONTINUITY`；呈现包缺 PTS、无 time_base、片尾无法确定 → `MEDIA_CLOCK_UNKNOWN`。向前的大间隔是合法的可变帧间隔，按呈现时间保留，不报错。片尾依次取：末帧 PTS + 该帧时长 → 流 start_pts + duration_ts → 容器 start_time + duration；容器时长若只等于末帧起点则视为未知，不估算 | 计划 7.2、7.3.5 |
| IF-13 | 时钟不可验证的媒体仍可登记（`clock.verified=false`、`extensions.clock_problem` 写明原因；`duration_ms` 与 `origin_pts` 因契约字段非空而写 0，并以 `extensions.duration_known=false` 标明不是测量值），但 MEDIA_PREP 拒绝为它生成任何依赖时间的产物。HDR/杜比视界/立体/多节目源同样可登记，MEDIA_PREP 报 `MEDIA_PROFILE_UNSUPPORTED` | 计划 6.4、7.4 |
| IF-14 | 复核/识别兼容视频：`-fps_mode passthrough`、`-enc_time_base <源时间基>`、libx264 CRF18 preset fast（取自请求快照的 media 设置）、yuv420p、`-bsf:v setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS`、`-video_track_timescale`、`+faststart`；旋转由 FFmpeg 默认的 autorotate 烘入；显示尺寸为奇数时只在右/下补 1 px（沿用 7.5 对 MP4 的同一条规则）。**不用 `setpts` 滤镜**：实测它会清掉帧时长，MP4 封装随后通过 edit list 把最后一帧裁掉；不指定编码时间基时 VFR 时间会被吸附到 1/帧率网格（实测最大偏差 11 ms）。派生完成后重新探测并逐帧比较源与派生的毫秒时间（容差 1 ms、帧数必须相同、首帧必须为 0），不通过即 `MEDIA_CLOCK_UNKNOWN(kind=derivative_drift)`，候选留在 `.pending`。需要 FFmpeg ≥ 5.1 且含 libx264，否则 `CAPABILITY_MISSING` | 计划 7.3.1、7.3.6 |
| IF-15 | ASR WAV：显式 `-map 0:<流序号>`，`-ac 1 -ar 16000 -c:a pcm_s16le`（无偏移时与旧模板的转换相同）。音轨起点取该流第一个**解码帧**的时间戳（取不到再用流 start_pts，都没有则 `MEDIA_CLOCK_UNKNOWN`），与视频原点的差用有理数算出后取整到毫秒：早于视频的部分 `atrim` 裁去，晚于视频的部分 `adelay` 补静音；音频短于视频时由程序在尾部补零样本到视频时长（`pad_after_ms` 是测量值）；音频长于视频不裁尾。全部写入 `derivation.json` | 计划 7.3.2–7.3.4 |
| IF-16 | 阶段缓存签名不再包含整个请求指纹。W02 的签名带 `request_fingerprint`，会让任何设置变化都重跑全部阶段，与 10.3 的失效表矛盾；改为每个阶段只声明真实依赖（内容身份、参数、工具身份、上游身份）。已验证：换 timing_mode / 换模式复用 OCR、ASR 证据，换 OCR 区域只重跑 OCR | 计划 10.3、FR02 |
| IF-17 | 角色到资源的绑定：默认 resource_id 为 `ffprobe`、`ffmpeg`、`whisper-cli`、`whisper-model`、`whisper-vad-model`、`magia-timeline`，可在 `engine_settings.resources` 里按角色改名。运行时的内容身份必须等于提交请求时冻结的 ResourceRef，否则 `RESOURCE_CHANGED`；提交之后才登记的资源在 resume 时接受。INPUT 阶段一次检查本请求到识别为止需要的全部角色，缺失时点名全部并且不启动任何进程 | 计划 6.2、10.4、W01 验收 |
| IF-18 | 阶段读取请求里冻结的 ConfigSnapshot（引擎设置、ASCII 临时根、gpu_profile），不读当前全局设置；只有资源的本机路径来自当前 `resources.local.json` | 计划 3.1"创建任务时固定完整有效配置快照" |
| IF-19 | 识别证据约定：父句 id `asr-NNNNNN`、视觉事件 id `ocr-NNNNNN`（同一产物内稳定）。Parent.score 只在原生 JSON 的每个词法 token 都带 `p` 时取其最小值（score_kind=`whisper_token_probability`，聚合方式写在 extensions），否则为 null。被拒绝的原生词时钟保留为 `timing_valid=false`，父句标 `WORD_CLOCK_REJECTED`，不插值。Magia 的占位文本 `Subtitle_Dialog_N` 记为空文本并标 `ENGINE_PLACEHOLDER_TEXT`。当前引擎只导出区间与文字，evidence_level 固定 `interval_only`。`recognize` 的 issues 使用 `ASR_EMPTY`、`ASR_WORD_CLOCK_UNUSABLE`、`ASR_SCORE_UNKNOWN`、`OCR_SCORE_UNKNOWN`、`OCR_EMPTY`，它们是给 W06 质量规则用的提示，不是错误码 | 计划 6.4、8.1、8.2、8.3 |
| IF-20 | Whisper 与 Magia 都在 `<ascii_temp_root>/asr|magia/<随机>` 下运行并在结束后清理；Magia 的 UTF-8 兼容副本缓存从程序目录 `runtime/magia_utf8` 移到 `<ascii_temp_root>/magia_utf8`（程序目录可替换、模型目录不写缓存）。引擎、模型、VAD 模型路径若含非 ASCII 字符，启动前报 `PATH_INVALID` 并要求重新绑定 | 计划 4.2、8.2 |
| IF-21 | URL 输入由锁定版本的 yt-dlp 模块作为自有 worker 进程执行（`roast worker yt-dlp …`，开发态为 `python -m upgrade.cli worker yt-dlp …`），固定参数 `--no-playlist --newline --write-info-json --merge-output-format mp4`；旧 `download_settings` 的画质表原样迁移，`cookies_from_browser` 只在用户原本显式选择了浏览器时迁移为 `download.browser_login_source`（引擎设置里不出现 cookie 字样的键，也不保存 cookie 内容） | 计划 7.1、12.3 |
| CO-CAND-003 | 需要负责人确认的接口补充：(1) contracts 新增 `Derivation`、`MediaManifest`、`MediaInput`、`MediaPlan`、`RecognitionRequest` 五种记录（后三种的字段来自 6.8，另加 `extensions`；前两种的字段集由本包确定）；(2) IF-10 的按 media_id 分目录布局；(3) CLI 内部子命令 `worker`（12.3 已规定 worker 经 `roast.exe worker …`，这里只实现了 yt-dlp 一个）；(4) `submit --video` 在已登记 ffprobe 时先做选流预检并把解析出的流序号写进请求 | 计划 0.2、6.8、12.1、12.3 |
| Q-W03-1 | **待负责人定**：末帧没有时长信息的 VFR MKV（容器时长只等于末帧起点）目前按规则报 `MEDIA_CLOCK_UNKNOWN`。是否接受，还是另行规定片尾取法，需要变更单；本包没有自行估算 | 计划 7.2、7.3 末段 |
| Q-W03-2 | **待 W09 在真实引擎上确认**的三个输出形态假设：whisper-cli 无语音时写出空 SRT 且 JSON `transcription` 为空；`-ojf` JSON 的 token 带 `p`；MagiaTimeline 无画面文字时写出没有 Dialogue 的 ASS（若它根本不写文件，当前按失败处理）。执行机没有这两个引擎，也没有 S04 的真实保存输出 | ENV-02、ENV-03、B01 |
| EV-03 | 新增 3 个测试文件 50 个用例 PASS；全套 30/30（3.10 venv）。媒体与队列级用例使用真实 ffmpeg + ffprobe；一条 URL 用例使用真实 yt-dlp 走回环 HTTP。没有 ffprobe 的环境里这些用例显示为 skipped，不计为通过 | `validation-ledger.json` T03-W03、T06-W03、FR01-URL-W03 |

## 5d. W04 记录（2026-11-10）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-22 | 第 5 节只列 `upgrade/translation.py`；W04 把分段接入放在 `upgrade/source.py`、词典版本放在 `upgrade/dictionary.py`、机器格式放在 `upgrade/subtitles.py`，四个阶段实现在 `stages.py`。公开 Interface 仍只有 `translate(TranslationRequest)`；其余是实现者可组织的私有模块 | 计划第 5 节"私有函数可由实现者组织" |
| IF-23 | 源版本布局：`revisions/source/<revision_id>/{manifest.json(SourceRevision), source.ja.srt, segments.json(SegmentSet), segments.v8.json, recognition/{speech_evidence.json, visual_evidence.json, reference.asr.ja.srt, reference.ocr.ja.srt, timeline.utf8.ass, evidence_refs.json}}`。`cue_id` 为 `cue-NNNNNN`，旧 v8 id 存 `legacy_cue_id`；Segment.role 由内容来源决定（语音范围+画面页 → mixed，仅语音 → speech，仅画面 → screen，visual_only 全为 screen）；v8 标记原样保留，父句词时钟被拒时追加 `WORD_CLOCK_REJECTED`+`CHECK`；visual_only 的 coverage 为 `not_applicable`。机器时间轴 ASS 随源版本保存，供任何以该源版本构建的机器 ASS 继承样式 | 计划 6.1、6.5、7.5、8.1、8.3 |
| IF-24 | supplied_source 在 W04 = 引用项目内已登记的 SourceRevision（目前只有机器生成的；人工源字幕导入属 W05）。不随请求给视频时，INPUT 用该源版本绑定的 media_id（`JobInput.source_media_id`，CLI 的 input value 为 `supplied-source:<revision_id>`）；给了视频但媒体身份不同 → `MEDIA_CHANGED`（QC21 语义），不翻译 | 计划 3.1、8.3、10.1、12.1 |
| IF-25 | 词典版本：`<DATA_ROOT>/dictionaries/<uuid>/{manifest.json(DictionaryRevision), files/{pre.txt,gpt.txt,post.txt,glossary.json,style_prompt.txt[,guideline.txt,project_config.yaml]}, diagnostics.json}`，按内容哈希寻址（同内容同版本）。输入来源依次为 `engine_settings.dictionary.<role>`（绝对路径）、迁移键 `translation.native_files.*`、程序内置默认（开发态 `app/config/`，冻结态 `<APP_ROOT>/defaults/`）；`use_native_dictionaries=false` 不收前置/GPT/后置词典；指引/项目配置只在 `native_prompt_mode≠不使用` 时收。不支持条目三类：条件/情景规则（`pre_jp` 等键）、缺少制表分隔的行、`^^`/`1^` 后为空的源（解析器静默丢弃，W04 列出）。确认记录在 `settings/dictionary-acceptances.json`（按内容哈希），请求快照保存被接受条目的 id 列表；术语表 JSON 非法或值非字符串/对象列为不支持项而不是静默空表 | 计划 8.5、D07 |
| IF-26 | 翻译：角色 `translation_engine`（默认 id `llama-server`）与 `translation_model`（默认 `sakura-model`）进入 INPUT 预检。llama-server 以旧命令行启动（`-m -ngl --port --host 127.0.0.1 --ctx-size --parallel 1`），每 2 秒检查进程/健康，进程退出立即失败，无配置时不设启动超时；端口已有监听者 → `SERVER_PORT_CONFLICT`（不接管、不关闭）；`/v1/models` 报的模型名与模型文件名不符 → 同样视为冲突并停掉自有进程。传输层：连接超时 10 秒、无生成截止、60 秒无新字节/内容发 `translation_waiting` 事件、取消时从监视线程中止 socket（Windows 上需关闭句柄）。检查点在 `runs/<run_id>/checkpoints/translation/{cues/<cue_id>.json, exchanges/NNNNNN/}`，按翻译签名归属。签名 = 分段文件哈希 + 源版本 id + 词典内容哈希 + 已接受条目 + 有效提示词哈希 + 引擎/模型内容哈希 + 生成参数（batch_size、context_size、gpu_layers、temperature、top_p、use_native_dictionaries、native_prompt_mode）；端口不进签名。`CONTEXT_EXCEEDED`：HTTP 400 且错误类型/文字表明上下文溢出时不重试；`finish_reason=length` 作为一次失败，3 次后同样报此码。TranslationRecord 的 `prompt_sha256` 是实际发送 messages 的规范 JSON 哈希；`postprocess_trace_ref` 指向整份 `postprocess_trace.json`。阶段结束无论成败都关闭自有服务；不跨任务常驻 | 计划 6.5、8.4、10.4、10.5 |
| IF-27 | 机器格式：SRT 块内不能表示空行，序列化时去掉文本中的空行（只做格式序列化，不改字）；ASS 事件统一用标准 `Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text`，样式表与 `[Events]` 之前的段落原样取自模板，`[Events]` 之后的段落放在新事件之后；每条事件的 Layer/Style/Name/Margin/Effect 取自它关联的 OCR 事件（按 visual_ids 的首个事件），无关联时用默认值（Style 取模板里的 `Default`，否则首个样式）。基础样式行 `Style: Default,Microsoft YaHei,80,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,200,1`（"其他字段与已审计 Default 行一致"按旧发布器的 Default 行取值，含 BackColour `&H80000000`） | 计划 6.7、7.5、9.5 |
| IF-28 | QUALITY_AND_COMMIT 在 W04 的范围：结构/身份门禁（QC01 回读解析、QC02 时间、QC03 边界〔SRT 1 ms、ASS 10 ms 量化〕、QC04 身份〔各阶段哈希与源版本一致〕、QC05 覆盖〔重新审计 v8 记录〕、QC06 顺序/编号、QC17 译文完整、QC23 空结果警示）→ 通过才提交 MachineRevision；`quality.json` 的 `unknown_capabilities` 按证据事实填（`ocr_scores`、`frame_pts`、`asr_scores`、`speech_activity`），`quality_scope.json` 列出未评估的规则。MachineRevision 的 `raw_evidence_refs` 收录版本目录内除四个具名文件以外的全部文件（HTTP 交换、译后痕迹、毫秒旁路、样式说明、构建报告、范围说明）。W06 实施全部规则时提升该阶段的 `STAGE_VERSIONS` 使旧缓存失效 | 计划 6.5、6.8、11、13 W06 |
| IF-29 | 边界：SOURCE 对 canonical 毫秒值用 1 ms 容差拒绝超出媒体末尾的 cue（与旧流水线 `validate_subtitle(media_duration_seconds)` 的失败行为等价，不钳制）；SUBTITLE_BUILD / 提交门禁回读文件时 SRT 1 ms、ASS 10 ms。`normalize_text` 后无可见字符的段 → `SUBTITLE_INVALID(kind=empty_source_text)` | 计划 6.7、11.2 QC03、8.3 |
| CO-CAND-004 | 需要负责人确认的接口补充：(1) contracts 新增 `SegmentSet`、`TranslationSet`、`DictionaryFile`、`DictionaryRevision`（字段集由本包确定）与 6.8 的 `TranslationRequest`；(2) `settings/dictionary-acceptances.json` 与 `dictionaries/` 的布局（4.2 只写了 `dictionaries/<dict_revision>/`）；(3) `queue run` 的返回值增加 `requests[]`（每个请求的状态、错误码、`machine_revision_id`、版本目录）、`submit` 的返回值增加 `dictionary_revision_id`；(4) `runs/<run_id>/checkpoints/` 目录；(5) 翻译角色进入 INPUT 预检（缺 llama-server 时连识别也不开始） | 计划 0.2、4.2、6.8、12.1 |
| Q-W04-1 | **待 W09 用真实 Magia 输出确认**：画面事件时间是 ASS 的 10 ms 量化值，而 11.2 对 canonical 值只给 1 ms 容差。若真实时间轴的末尾事件超出媒体末尾 2–9 ms，SOURCE 会按 QC03 拒绝整个任务。本包按字面规则实施，没有自行放宽 | 计划 11.2 QC03、6.7 |
| Q-W04-2 | **待 W09 在真实引擎上确认**的假设：llama-server 提供 `/health`、`/v1/models` 的 id 为模型路径（旧 `require_server_model` 的核对方式）、SSE `data:` 事件带 `finish_reason` 与 `[DONE]`、上下文溢出返回 HTTP 400 且 `type=exceed_context_size_error`（取自 llama.cpp 服务端源码，未在执行机上验证） | ENV-03、B01 |
| Q-W04-3 | visual_only 的每条 cue 带 v8 的 `VISUAL_ONLY`+`CHECK`（算法对"无语音匹配"的标记，在该模式下是设计使然）。本包原样保留；W06 实施 QC19 时需决定该模式下是否视为"不确定" | 计划 8.3、11.2 QC19 |
| EV-04 | 新增 4 个测试文件 39 个用例 PASS，阶段测试改为八阶段 14 个用例 PASS；全套 34/34（3.10 venv，两次运行一致）。翻译协议用例使用真实回环 HTTP 与真实子进程（脚本化的模型服务替身），不是真实模型 | `validation-ledger.json` T03-W04、T04-W04 |

## 5e. W05 记录（2026-11-10）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-30 | 模块落位：`upgrade/revisions.py` 的公开 Interface 即第 5 节的 `create_session`、`import_revision`、`attest_revision`、`resolve_revision`；`import_revision(kind="review"|"source")` 一个入口同时服务 `review import` 与 `source import`。`upgrade/quality.py` 提前在本包建立，只含本包要用的规则（QC01/02/03/06/20/21/23）与人工状态计算；其余规则、阈值与 review.csv/html 属 W06。W04 的 `stages.py`、`source.py` 没有改：人工源版本直接走已有的 `supplied_source` 路径 | 计划第 5 节；W05“改动面” |
| IF-31 | 布局：`reviews/sessions/<session_id>/{base.json(ReviewSession), edit.srt|ass}`；`revisions/review/<revision_id>/{manifest.json(ReviewRevision), original.srt|ass, view.json(SubtitleView), correspondences.json(CorrespondenceSet), quality.json(QualityReport)}`；人工源版本 `revisions/source/<revision_id>/{manifest.json(SourceRevision, origin=human), original.srt|ass, segments.json(SegmentSet), view.json, correspondences.json, quality.json}`（view/correspondences/quality 记在 `recognition_refs`：人工源版本没有识别证据，该字段在这里承载解析与核对证据）；`reviews/attestations/<attestation_id>.json`；未提交候选 `.pending/<id>/{original.<ext>, view.json, correspondences.json, quality.json, candidate.json}` | 计划 4.2、9.1–9.4 |
| IF-32 | 快照协议：只读一次字节；复制前、复制后、两次哈希之后共三次比对源文件的大小与修改时间，并比对副本哈希与源文件哈希、副本长度与复制前大小；任一不符或 `PermissionError` → `REVIEW_FILE_BUSY`（可重试），不循环等待、不留候选；提交前再核对一次 `.pending` 里副本的哈希。文件上限取 `subtitles.MAX_SUBTITLE_BYTES`，超过 → `SUBTITLE_INVALID(kind=too_large)` | 计划 9.2 步骤 2、4.3 |
| IF-33 | 编码：UTF-8、UTF-8 BOM、带 BOM 的 UTF-16 LE/BE 自动识别；其他编码必须由用户用 `--encoding <Python codec>` 给出并严格解码，否则 `SUBTITLE_INVALID(kind=unknown_encoding)` 并保留候选。不猜编码、不用 `errors=ignore/replace`、不转码保存；manifest 记 `encoding` 与 `encoding_source`（`utf-8`/`bom`/`user`） | 计划 9.2 步骤 3、D08 |
| IF-34 | 对应关系口径：`exact` = 格式相同、内容键相同、起止时间相同，且在基础文件与人工文件里都唯一。内容键：SRT 为显示文本；ASS 为事件类型加除 Start/End 外的全部字段原文。`candidate`（只给人看，不参与配对/重翻/覆盖）有三种原因：`identical_but_not_unique`、`same_time_content_changed`、`same_content_time_changed`；其余 `unmapped`（`no_identical_cue`、`invalid_time`、`no_base`、`format_differs`）。`provenance_status`：没有 exact 或没有基础版本 → `unmapped`；全部 exact 且基础文件每条都被对上 → `exact`；否则 `partial`。`source_cue_ids` 在能确定时用基础版本自己的 cue id（`source_cue_id_scheme=revision_cue_id`），否则用基础文件视图的 cue id（`base_view_cue_id`） | 计划 9.2 步骤 4、6.6 |
| IF-35 | 人工源分段：每条有可见文字的 Dialogue 一个分段，`timing_source=human`、`source_ranges=[]`、`visual_ids=[]`、`role=unknown`、`flags=[human_authored]`、新的 uuid cue id、按开始时间排序（`ordered_by_start_time` 记录是否重排过）；Comment、绘图事件、无可见文字的事件列入 `omitted_events`。即使某条与基础源版本 exact，旧的字符范围/词时钟也不复制到新分段，只在 `correspondences.json` 留一行参考。`SegmentSet.coverage.status=human_authored, audited=false`。超出片尾的容差按源版本毫秒值取 1 ms（同 IF-29） | 计划 9.4、D10、W05 验收“原文修改不复用错词时钟” |
| IF-36 | 确认：一条 Attestation 绑定“版本 id + 文件哈希 + 媒体 id”，范围只能是明确列出的 `text`/`timing`/`style`（去重后按固定顺序保存），SRT 不接受 `style`，没有 `all`。状态现场计算：SRT 需 text+timing、ASS 需 text+timing+style 才是 `REVIEWED`；人工稿默认 `PARTIAL`，机器版本默认 `UNREVIEWED`。机器版本有 SRT 与 ASS 两个文件，确认只落在 `--format` 指明的那一个上；不是合法 Attestation 的文件不计入并以 `ATTESTATION_UNREADABLE` 提示 | 计划 3.3、6.6、D19 |
| IF-37 | `resolve_revision` 返回 `ResolvedRevision`：`kind`、`media_id`、项目内相对目录、manifest、逐格式的文件引用（已核对哈希）、逐格式的确认状态、`notices`。提示码：`BASE_SUPERSEDED`（该人工稿最终依赖的机器版本之后，同一媒体又提交了新的机器版本；按项目索引顺序判断，不看文件时间）、`PROVENANCE_PARTIAL|UNMAPPED`、`ATTESTATION_UNREADABLE`。机器版本自身不算“被更新”。CLI 把 notices 原样放进 `OperationResult.warnings` | 计划 9.3、6.6、12.1 |
| IF-38 | 拒绝口径：校对副本或基础版本属于另一媒体 → `MEDIA_CHANGED`；项目里没有该媒体 id → `INPUT_MISSING`；两者都带 `rule_id=QC21_MEDIA_BINDING`。结构错误（QC01/QC02/超容差的 QC03）→ `SUBTITLE_INVALID`，文件与报告留作候选，不建版本。QC03 的定位指向字幕块首行。中文稿的基础只能是机器版本或另一份中文稿，源字幕的基础只能是源版本，否则 `CONFIG_UNSUPPORTED_COMMAND`。版本没有所要的格式 → `NEEDS_SELECTION`，不现场转换。参数解析错误改为 stdout 一份 `OperationResult(operation=arguments)` + 退出码 2（`--help` 不变） | 计划 9.2、11.2 QC21、12.1 |
| IF-39 | 同一文件重复登记不去重：每次登记都是新的不可变版本（计划 9.2“每次导入都是新 revision”）。`BASE_SUPERSEDED` 的人工稿不被移动、不被改基、不被合并；据它再建副本得到的仍是它自己的字节 | 计划 9.2、9.3、D09 |
| CO-CAND-005 | 需要负责人确认的接口补充：(1) contracts 新增 `SubtitleView`、`ViewCue`、`CorrespondenceSet`、`ResolvedRevision`、`QualityInput` 的字段集（6.6/6.7 只给了名字或部分字段）；(2) `ReviewRevision.extensions` 的键（`encoding_source`、`imported_file`、`base_format`、`superseded_by_at_import`、`source_cue_id_scheme`）与人工 `SourceRevision` 用 `recognition_refs` 承载解析/核对文件；(3) 12.1 没写的选项：`review import|source import --encoding`、`review import --base`、`review attest --format|--note`；(4) `source import` 的返回值带 `next_command`（提示下一步，不自动执行）；(5) 参数错误的 `operation=arguments` 结果。在负责人确认前按上述实现，全部可在不动已提交版本的前提下调整 | 计划 0.3、6.6、6.7、12.1 |
| Q-W05-1 | **待 W09 用真实编辑器确认**：执行机没有 Subtitle Edit 与 Aegisub，本包的 SRT/ASS 样本按两者的文件形态手写（CRLF、BOM、`[Aegisub Project Garbage]`、Comment 行、覆盖标签、绘图）。未覆盖：Aegisub 的 `[Aegisub Extradata]`、Subtitle Edit 以 ANSI 保存的选项、两者保存过程中的真实占用方式（本包用真实的 Windows 字节范围锁和三次比对验证了协议，不等于验证了编辑器） | ENV-03、B05、计划 14.3 T05/T08 |
| Q-W05-2 | W05 验收“旧 base 更新仍可导出旧人工稿”在本包验证到：哈希核对后的 `resolve_revision`、确认状态不变、仍可建立副本；“导出”动作本身在 W07 实现并验证。T05 的“旧任务副本迁移后旧 hash 不变”属 W08 | 计划 W05 验收、W07、W08 |
| Q-W05-3 | 结构错误的候选按 4.2/9.2 保留在 `.pending/`，目前没有列出/丢弃它们的命令；W08 的界面需要提供（否则只能手工删除目录） | 计划 4.2、9.2 |
| EV-05 | 新增 `tests/upgrade_revisions_test.py` 15 个用例 PASS（14 个建立在真实 FFmpeg/ffprobe 流水线产出的机器版本上，命令行用例经真实子进程，文件占用用例用真实的 Windows 文件锁；1 个静态核对导入关系）；全套 35/35（3.10 venv） | `validation-ledger.json` T05-W05 |

## 5f. W06 记录（2026-11-11）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-40 | `quality.evaluate(QualityInput)` 返回 `{report, scope, rows}`；计划接口 `check_quality()` 不变，只返回 `QualityReport`。模块不打开任何文件，识别证据、分段、译文和后处理记录由调用方放进 `extensions.loaded` | 计划 5 节模块表、6.8 |
| IF-41 | 规则范围另存 `quality_scope.json`：`evaluated_rules`、`not_evaluated_rules`（规则 → 原因）、`disabled_rules`（阈值为 null）、`hard_rules`、`rules_version=2`、阈值快照。没有检查的规则在报告里写"未检查"，从不显示为通过 | 11.1、D19 |
| IF-42 | 机器版本的规则表跑在机器 SRT 的只读视图上，层号取自同版本 ASS（条数一致时）；ASS 的结构由 W04 的回读校验负责 | 6.7、11.2 QC09 |
| IF-43 | QUALITY_AND_COMMIT 遇 ERROR 不提交；报告四件套写到本次尝试目录 `quality_refused/`；错误码：QC05 → `SOURCE_COVERAGE_ERROR`，QC04/QC17 → `TRANSLATION_IDENTITY_ERROR`，其余 → `SUBTITLE_INVALID`，`details.rule_ids` 列出全部规则 | 11.1"允许原样取出失败输入用于修复" |
| IF-44 | `STAGE_VERSIONS["QUALITY_AND_COMMIT"]=2`、`QUALITY_GATE_VERSION=2`，旧质量阶段缓存失效（IF-28 结算）；同一运行、同样字幕与译文字节的已有机器版本，只有其 `quality_scope.json` 的 `rules_version` 与阈值都相同才复用，否则提交新版本，旧版本不动 | 4.3、IF-28 |
| IF-45 | 人工中文版本 manifest 的 `extensions.report_refs` 登记 `quality_scope.json`、`review.csv`、`review.html`，与其他文件一样按哈希校验；人工源版本沿用 W05 规则（其余文件进 `recognition_refs`） | 4.3、6.6 |
| IF-46 | 人工稿报告的逐条来源：角色只取 exact 对应；candidate 只显示基础分段的源文并标 `partial`；其余为 `unknown` / `unmapped`，不猜 | 9.2 步骤 4、11.3 |
| IF-47 | `review.csv`：每条显示字幕一行，不属于任何字幕的问题各一行；`rule_ids` 用 `;`、`message` 用 ` \| ` 连接，`severity` 取最严重；时间写作 `HH:MM:SS.mmm`；全部单元格加引号、CRLF；公式前缀按 11.3 | 11.3 |
| IF-48 | `review.html`：`Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:`，没有脚本；链接为相对路径并百分号编码；逐条列出规则是否检查 | 11.3 |
| IF-49 | 分数阈值只用于登记过类型的分数：ASR `whisper_token_probability`（计划原名）；OCR 登记一个名字 `ocr_confidence_0_1`（0—1、越高越可信、未校准）。目前没有后端产出它（B03），真实运行中 OCR 只会出 QC12 汇总 | 6.4、11.2 QC10/QC12 |
| IF-50 | 边界帧：每个时间点导出"当时在屏幕上的帧"和"前一帧"；寻址点取两帧 PTS 的中点并按文件起始时间换算；`-copyts` 下以 `showinfo` 报告的 PTS 与包探测的呈现 PTS 核对，不一致即 `EXPORT_VALIDATION_FAILED`；媒体时钟未验证即 `MEDIA_CLOCK_UNKNOWN` | 6.4"frame ref 必有实际 PTS"、11.3 |
| CO-CAND-006 | **按需导出边界帧的命令与位置**：计划只要求"边界帧按需导出"，没有给命令和目录。本包实现为 `review frames --data-root --project --revision (--issue ID \| --at-ms 列表) --destination 新目录`，写到用户指定的新目录（版本目录只创建一次，不能事后加文件），PNG 文件名带 PTS 与时间基，另附 `frames.json`。待负责人确认命令形态或改为项目内固定目录 | 4.2/4.3/12.1 未定义 |
| Q-W06-1 | QC18 的"未确认译名"：现有翻译后处理没有这种标记，本包不新增检测，只报告"缺译名"和"残留假名"两种已有标记。需要时由负责人定义（与 B06 一起） | 8.4、11.2 QC18 |
| Q-W06-2 | QC19 的"未完句"：没有现成检测；11.2 注明不实现更复杂的语义判断，本包只报告现有标记与 `[待确认]` | 11.2 注 |
| Q-W06-3 | QC09 遇到没有角色信息的外部人工文件：按"unknown 提示"处理为 WARNING、`verification=unknown`；如负责人认为应降为 INFO，只改这一处 | 11.2 QC09"无role的外部文件按unknown提示" |
| Q-W04-3 | **结算**：`visual_only` 模式下全部 `VISUAL_ONLY`+`CHECK` 的分段是模式事实，汇总成一条 QC19 INFO；其他模式下仍逐条 WARNING | W04 记录 |
| Q-W04-1 | 仍待负责人决定（ASS 10 ms 量化的画面时间与 1 ms 规范容差），本包按字面实现：ASS 文件用 10 ms 容差，机器 ASS 与分段的时间比较允许 9 ms | W04 记录 |
| EV-06 | 证据：`tests/upgrade_quality_test.py`（18 个用例，表驱动 + 真实 ffmpeg 边界帧 + 真实流水线）、`tests/upgrade_stages_test.py`；全套汇总 `LOCAL_WORKSPACE/roast-w06-tests-summary.json` | 14.3 T03 |

## 5g. W07 记录（2026-11-11）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-51 | 导出流程放在新模块 `upgrade/export.py`（计划第 5 节写作 `media.export_media`）：`media.py` 已承担探测/派生/边界帧，且只有 `cli.py` 可以引入 `revisions`，所以由命令行解析版本后把 `ResolvedRevision` 交给 `export.run_export()` | 5 节模块表、W05 隔离规则 |
| IF-52 | 导出目录就是用户的 `--destination`（必须是新目录；先写同级 `.<名>.pending-<id>/`，校验后整体改名）；项目内 `exports/<export_id>/location.json` 记录位置、manifest 哈希、版本与类型，`project.json.export_ids` 追加 | 4.2、4.3、6.6 `destination_root` |
| CO-CAND-007 | **导出文件的存放位置**：计划 4.2 把 `exports/<export_id>/` 列在项目目录里，同时 ExportRequest 有 `destination_root`。本包把输出直接写到用户选的目录、项目里只留位置记录，避免成片在数据根里再存一份。待负责人确认；若要求项目内保存，改 `_record` 与目标路径即可 | 4.2 与 6.6 的两种写法 |
| IF-53 | 视频导出一律 `-copyts`；字幕零点 = 首个显示视频帧的时间戳。硬字幕：首帧时间不为 0 时在 `subtitles` 滤镜前后各用一次 `setpts` 平移；软字幕：`-itsoffset <首帧时间>` 只作用于字幕输入。事实：不用 `-copyts` 时 MKV 封装器会为 AAC 预滚把所有流整体后移约 21 ms | 6.3 规范时钟、7.5 |
| IF-54 | 机器版本（同时有 SRT 与 ASS）导出视频时缺省用 ASS（保留层与样式），`--format srt` 可改；字幕导出缺省两个都给 | 9.5"机器版同时提供SRT/ASS" |
| IF-55 | MKV 字幕轨语言写作 `chi`（Matroska 使用 ISO 639-2 代码；计划写作"zh"） | 7.5 `mkv_soft_v1` |
| IF-56 | 字体：只查 `--font-dir` 与 Windows 系统/用户字体目录；按 `name` 表（ID 1/4/6/16）匹配，按 `cmap` 格式 4/12 检查字形；样式字体检查用该样式的字幕文字，行内 `\fn` 字体按整条字幕文字检查（只会多报）。硬字幕把匹配到的字体文件复制进 ASCII 工作目录 `fonts/` 供 libass 使用；软字幕不附任何字体 | 7.5 字体段、B07 |
| IF-57 | 格式转换只在 `--convert-to` 明确给出时进行：ASS→SRT 有绘图事件时还需 `--accept-omitted-drawings`；派生文件名 `<名>.derived.<格式>`，`lossy` 如实标注，状态 `UNREVIEWED`，原确认只以 `referenced_confirmations` 引用；机器版本拒绝转换（已有两种格式） | 9.5 |
| IF-58 | 输出校验项目：流数量与编码、默认/强制/语言、时长容差 `max(最大帧间隔, 50 ms)`（最大帧间隔取源的 `frame_timing`）、软字幕提取比对（相对输出首帧；ASS 10 ms、SRT 1 ms）。失败一律 `EXPORT_VALIDATION_FAILED`，候选保留 | 7.5 |
| IF-59 | 编码进度用 `-progress pipe:1` 写入进程日志；本包没有界面进度（W08） | 7.5 |
| IF-60 | QC21 在导出时检查：`--media` 与字幕绑定的媒体不同 → `MEDIA_CHANGED`（`rule_id=QC21_MEDIA_BINDING`）；QC22 由导出时的字体检查驱动（成片 ERROR / 字幕 WARNING），结果写入 `quality_snapshot.export_issues` | 11.2 QC21/QC22 |
| EV-07 | 证据：`tests/upgrade_export_test.py`（7 个用例：真实八阶段流水线产出 + 真实 FFmpeg/ffprobe；合成画面，含 S06 形态的人工 ASS）；全套汇总 `LOCAL_WORKSPACE/roast-w07-tests-summary.json` | 14.3 T06 |

## 5h. W08 记录（2026-11-11）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-61 | 旧窗口只新增一个“新版项目与校对”选项卡（`_build_v2_tab`），由 `upgrade/gui.py` 的 `V2Panel` 提供；v2 代码加载失败时旧窗口照常工作 | D01、3 节"不更换现有窗口框架" |
| IF-62 | 面板的每个操作都通过子进程运行 `python -m upgrade.cli` 并显示其 JSON 结果；面板只读项目索引，不引入 `revisions` | 12.1、W05 隔离规则 |
| IF-63 | 面板设置（数据根、编辑器路径）存在 `app/config/roast_v2_gui.local.json`，不读写旧配置 | W08 验收"旧配置未重写" |
| IF-64 | 编辑器以分离进程启动，参数只有校对副本路径；不等待、不监视、不保存进程句柄；未配置时让用户选，取消则打开副本所在文件夹 | 3.2"用编辑器打开"、D03 |
| IF-65 | `revisions list`（只读）供窗口列出版本；`migrate --data-root DIR --legacy-job DIR [--display-name] [--no-media]`（计划 12.1 写作 `--config FILE`；为与其他 v2 命令一致使用 `--data-root`） | 12.1 |
| IF-66 | 迁移：小文本文件（json/jsonl/srt/ass/ssa/csv/txt/log，≤ 20 MiB）逐字节复制到 `legacy/files/`，其余文件只记哈希；原视频登记为媒体（失败只记提示，不影响迁移）；旧字幕只列为 `UNREVIEWED_LEGACY_SNAPSHOT` | 9.6 |
| Q-W08-1 | ~~面板尚未提供“新任务”对话框与队列操作、资源重新定位与词典警告确认的界面~~ **已结算（W08 第二部分，同日）**：见 IF-67…IF-74；W08 改为 COMPLETED（真实编辑器往返仍属 T08/W09） | 3.1、3.2、W08 任务 |
| EV-08 | 证据：`tests/upgrade_gui_migrate_test.py`（7 个用例：按钮规则、状态措辞、旧窗口静态核对、真实数据根上驱动面板、迁移与旧目录不变）；全套汇总 `LOCAL_WORKSPACE/roast-w08-tests-summary.json` | 14.3 T05/T08 |
| IF-67 | 面板分两个子选项卡：“任务队列”（新任务、队列列表与操作、资源与工具）和“项目与版本”（第 3.2 节任务详情按钮，新增“打开原视频”）；数据根与编辑器设置两边共用 | 3 节 |
| IF-68 | **停止当前 = `queue stop`**：写 `queue/stop.flag`；执行器取消当前请求的自有进程，该请求记为 `INTERRUPTED`（错误 `PROCESS_CANCELLED`，`details.kind=queue_stop`，`stage`=被停的阶段），队列暂停，下一个请求保持 `QUEUED`；之后“恢复中断任务”沿用原配置新增 attempt，已提交阶段不重跑。**跳过当前 = 对运行中请求 `queue cancel`**：记为 `CANCELLED`，队列接着处理下一个。没有执行器时 `queue stop` 不写任何文件；执行器启动时清掉遗留的停止标记（开始队列是用户的新决定） | 3.2、10.2、10.5 |
| IF-69 | 队列列表 = `queue list`（只读）：全部请求（被移除的也保留为历史）、显示名、输入、模式、执行状态、当前阶段与已耗时、机器版本、人工版本数、质量状态。目前没有阶段提供可靠的 processed/total，`progress` 恒为 `null`，界面不显示百分比；后台导出/导入也只显示已耗时 | 3.2“不估造整体百分比” |
| IF-70 | 复制为新任务 = `queue copy`：同一项目、同输入/模式/时间轴/选流/复制原片选项，配置快照、词典版本与资源引用取当前；原请求的快照不变；结果带 `config_changed` | 3.1“复制为新任务才能采用新配置” |
| IF-71 | 新任务对话框：来源（本地视频/单视频 URL）与显示名必填，显示名只作为一个参数传给命令；四种模式固定，联合分析附“画面驱动不保证原文全部来自画面识别”的说明。多视频流/多音轨、无音轨、词典不支持条目都由命令先拒绝，再作为问题交给用户（选流、改模式、确认条目），按回答重跑命令；不自动换模式、不默认第一条音轨。“已有日文源字幕”依次 `media register`（先定流再建项目）→ `source import` → `submit --mode supplied_source`。只有点“加入队列并开始”才启动执行器 | 3.1、8.5 |
| IF-72 | 打开原视频 = `media locate`（找不到 → `INPUT_MISSING`，内容不同 → `MEDIA_CHANGED`）→ 用户选现在的位置 → `media relocate`：完整 SHA-256 必须等于登记值，否则 `MEDIA_CHANGED`（要作为新媒体登记）；复制进项目的原片不需要重新定位 | 3.2“重新定位媒体”、10.3 |
| IF-73 | 资源与工具窗口 = `resources list`（不运行版本探针）+ `resources rebind`；内容身份变化的资源只能重新登记，不能只改路径 | 3.4、10.3 |
| IF-74 | 队列执行器由窗口以子进程启动（`queue run`）。关闭窗口时若队列或后台导出/导入仍在运行，只提供“取消当前并退出”（先 `queue stop`，最多等 20 秒，再终止自己的子进程；被中止的导出不发布成品，可能留下 `.pending` 临时目录）或“返回”；其他窗口/命令行启动的执行器不受影响。限制：窗口进程异常崩溃时执行器不会随之结束，下次启动按 10.2 把它的请求恢复为 `INTERRUPTED` | 10.5 |
| CO-CAND-008 | **命令补充**（12.1 未列）：`queue stop`、`queue list`、`queue copy`、`media register`、`media locate`、`media relocate`、`resources list`；`queue run` 的结果多一个 `paused`。另：“停止当前”的请求状态取 `INTERRUPTED`（可恢复）而不是 `CANCELLED`，以区别“跳过当前”。待负责人确认 | 3.2、12.1 |
| EV-08b | 证据：`tests/upgrade_queue_gui_test.py`（18 个用例：列表文字与按钮规则、停止/跳过/遗留标记的队列语义、`queue copy/list/stop` 与 `media`/`resources` 命令、真实数据根上驱动面板：新任务的选流与词典确认、无音轨不改模式、自带日文字幕、执行器作为窗口子进程运行并恢复/复制/移除、打开原视频与重新定位、关窗只给两种选择）；全套汇总 `LOCAL_WORKSPACE/roast-w08b-tests-summary.json` | 14.3 T02/T05/T08 |

## 6. 不做的事（W00 重申）

不删除 VoiceTransl/剪映相关源码，不为商用换模型，不更新全局 Python，不重写分段算法，不下载模型，不修改 D 盘旧目录、原媒体、旧词典，不在用户文档目录写入任何文件。
