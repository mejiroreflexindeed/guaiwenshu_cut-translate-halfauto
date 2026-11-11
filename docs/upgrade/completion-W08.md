# completion-W08

```text
工作包：W08 GUI 整合和旧项目迁移；计划版本：1.1；基线提交：1a9a0104…（W07 提交 841b26d）
需求：FR01、FR05、FR09、FR13、FR16；决策：D01、D03、D09、D16、D19 相关；验证：T05（迁移部分）、T08（界面部分，真实编辑器属 W09）
里程碑：无
状态：COMPLETED——第一部分（提交 db4fd04）：任务详情按钮、状态措辞、原文修订分支、显式导出与旧任务迁移；第二部分（同日，Q-W08-1 结算）：新任务对话框、队列列表与六个操作、打开原视频/重新定位媒体、资源重新定位、词典警告确认、关窗确认。T08 中真实 Subtitle Edit / Aegisub 往返需要编辑器（B05），随 W09 验收
```

## 实现

用户可观察的新行为：

| 行为 | 说明 |
| --- | --- |
| 旧窗口多了一个“新版项目与校对”选项卡 | 现有 tkinter 窗口不换框架（D01），只新增一个选项卡；v2 代码加载失败时选项卡里显示原因，旧窗口其余部分照常工作 |
| 项目与版本列表 | 选数据根后列出项目（旧任务导入的项目会标注）和每个项目的机器/日文源/人工版本：格式、创建时间、内容哈希前 12 位、人工状态、质量状态 |
| 第 3.2 节的按钮 | 打开输出目录、查看复核报告、创建校对副本、用编辑器打开、登记校对稿、修订日文原文 / 登记原文修订、据此生成新机器译文、导出字幕、导出视频、标记人工确认、导入旧任务。每个按钮只在规定条件下可用（见下表） |
| 状态措辞 | 机器版本未确认时显示“机器字幕已生成，等待人工检查”；登记的人工稿显示“部分确认（确认范围为空）”；只有用户确认了所需范围才显示“已人工确认”。界面上没有“模型完成 = 已审”的标签 |
| 确认对话框 | 文字/时间/样式三个框默认都不勾；SRT 的样式显示“（不适用）”且不能勾；不勾任何一项不会写确认 |
| 编辑器 | 只打开校对副本（session 的 edit 文件）；没配置编辑器时先让用户选，取消则只打开副本所在文件夹；编辑器是独立进程，程序不等待、不监视、不因编辑器关闭而自动登记，关闭本窗口也不会结束编辑器 |
| 原文修订分支 | “修订日文原文”建立日文源字幕的校对副本 → 改好后“登记原文修订”成为新的源版本 → 只有再点“据此生成新机器译文”才会提交 supplied_source 任务并运行队列 |
| 导出 | 必须先选中一个具体版本；有两种格式时导出字幕可都要或只要选中的一种；导出视频在对话框里选 MP4 硬字幕/MKV 软字幕与音轨，输出总是新建文件夹 |
| `roast migrate` / “导入旧任务” | 把一个旧任务目录只读导入为新项目：`legacy/source-manifest.json`（源目录、导入时间、每个文件的哈希、旧状态与各阶段状态、旧版本信息、原视频引用）、小文本文件的字节副本 `legacy/files/`；视频只引用并（可选）登记为媒体；旧 `final.ass` / `zh_machine.srt` 只列为“未审旧快照”；旧 DONE 不变成新 DONE；不从修改时间猜是否手改，手改过的文件由用户用“登记校对稿”明确登记（来源未审计） |
| `roast revisions list` | 只读列出一个项目的版本（窗口用它，也可直接在命令行用） |
| 两个子选项卡（第二部分） | “任务队列”：新任务、队列列表与操作、资源与工具；“项目与版本”：上面这些任务详情按钮，另加“打开原视频” |
| 新任务对话框（3.1） | 输入来源（本地视频 / 单个视频的 URL）和项目显示名必填；识别模式四选一（语音与画面联合分析〔默认〕/ 仅语音 / 纯画面文字 / 已有日文源字幕），联合分析可选时间轴（混合〔默认〕/ 画面驱动 / 语音驱动）并显示“画面驱动不保证原文全部来自画面识别”的说明；可选“把原视频复制进项目”；按钮“只加入队列”“加入队列并开始” |
| 选流、无音轨、词典条目 | 文件有多个视频流或音轨时弹出选择（不默认第一条，联合分析/仅语音不提供“不要音轨”）；所选模式需要声音而文件没有音轨时提示改选“纯画面文字”，不替用户改；词典里有新版不支持的条目时逐条列出（文件、行号、原文、原因），用户点“接受并继续”才提交，同一份词典内容只问一次 |
| 已有日文源字幕 | 先登记原视频（选流有歧义时先问，问清前不建项目），再登记日文字幕为源版本，最后提交“已有日文源字幕”任务 |
| 队列列表（3.2） | 名称、输入、模式、执行状态、当前阶段、机器版本、人工版本数、质量状态；运行中显示“阶段（已 X 分 Y 秒）”，不显示估算百分比；执行器运行时每 2 秒刷新。状态写法：排队中 / 运行中 / 已完成 / 失败（错误码，可恢复）/ 已停止（可恢复）/ 已中断（可恢复）/ 已跳过 / 已移除（未开始） |
| 队列操作（3.2） | 开始队列、停止当前、跳过当前、移除未开始请求、恢复中断任务、复制为新任务（规则见下表）；跳过和移除先确认 |
| 打开原视频 / 重新定位媒体 | 原视频还在原处且内容一致就直接打开；找不到或内容不同时问用户是否指向新位置，只接受完整哈希相同的文件 |
| 资源与工具 | 列出已登记的工具和模型及状态（正常 / 找不到 / 内容和登记时不同）；“重新定位…”把移动过的资源指向新位置（内容必须一致） |
| 关窗确认（10.5） | 队列或后台导出/导入还在运行时，关闭旧窗口只给“取消当前并退出”或“返回”两个选择 |
| 后台耗时 | 导出视频、旧任务导入等后台操作在状态栏显示已耗时，结束时写出总用时 |

按钮条件（`gui.action_states`，纯函数，单独测试）：

| 按钮 | 可用条件 |
| --- | --- |
| 打开输出目录 | 选中一个可读的已提交版本 |
| 查看复核报告 | 该版本有 `review.html` |
| 创建校对副本 / 标记人工确认 | 选中版本并选定格式（有两种格式时必须明确选一个） |
| 用编辑器打开 | 本窗口建立的校对副本文件存在 |
| 登记校对稿 | 选中一个版本（取它的媒体）；文件可选校对副本或任意外部 SRT/ASS |
| 修订日文原文 | 选中机器版本或日文源版本；进行中时变为“登记原文修订” |
| 据此生成新机器译文 | 本窗口刚登记了一个新的日文源版本 |
| 导出字幕 / 导出视频 | 选中版本且结构检查通过 |
| 导入旧任务 | 数据根有效 |
| 打开原视频 | 选中的版本有媒体 |

队列按钮条件（`gui_queue.queue_action_states`，纯函数，单独测试）：

| 按钮 | 可用条件 |
| --- | --- |
| 新任务… / 资源与工具… | 数据根有效且有 `settings/app.json` |
| 开始队列 | 没有执行器在运行（本窗口或别处）且有排队请求 |
| 停止当前 | 有执行器在运行、有运行中请求且还没请求停止 |
| 跳过当前 | 有执行器在运行且有运行中请求 |
| 移除未开始请求 | 选中的请求是排队中 |
| 恢复中断任务 | 选中的请求是失败或已中断（已停止） |
| 复制为新任务 | 选中任一请求 |

机制：

- **界面只是命令行的前端**：每个按钮运行 `python -m upgrade.cli …` 并显示它的 JSON 结果，所以界面做不到命令行拒绝的事（没有 `latest`、不会替用户确认、不覆盖）。`gui.py` 只读项目索引来列项目，不引入 `revisions`（W05 的隔离规则继续成立）。耗时的导出视频、翻译队列、旧任务导入在后台线程运行，结果回到界面线程显示。
- **设置**：面板自己的数据根与编辑器路径存放在 `app/config/roast_v2_gui.local.json`（已被 `.gitignore` 排除），旧的 `roast_pipeline.local.json` 不被读写。
- **迁移**：先读 `task.json` 判断是不是旧任务（不是则 `INPUT_MISSING`，不创建项目）；复制前后对旧目录逐文件比对哈希与大小，中途有变化即 `ARTIFACT_CHANGED` 并只删除新的 `.pending`；旧目录从不写入。
- **停止与跳过**（IF-68）：“停止当前”运行 `queue stop`，在数据根写一个停止标记；执行器看到后取消当前请求的自有进程（约 3 秒宽限后终止进程树），把它记为 `INTERRUPTED`（可恢复），并且不再取下一个请求（队列暂停）。“跳过当前”是对运行中请求的 `queue cancel`，记为 `CANCELLED`，队列继续。恢复沿用原配置、同一个 run 新增 attempt，已提交的阶段不重跑。
- **问题与重跑**（IF-71）：界面从不猜选流或替用户接受词典条目——命令先拒绝（`NEEDS_SELECTION` 带候选、`DICTIONARY_UNSUPPORTED` 带条目），界面把它变成问题，用户回答后带上 `--video-stream/--audio-stream/--accept-dictionary-warnings` 重跑同一个命令。
- **执行器归属**（IF-74）：“开始队列”由本窗口以子进程运行 `queue run`，输出管道由后台线程读完，结果回到界面线程显示；后台线程的回调统一经过界面线程的队列执行。关窗时先 `queue stop`，最多等 20 秒，再终止自己的子进程。
- **新命令**（CO-CAND-008）：`queue stop | list | copy`、`media register | locate | relocate`、`resources list`。`submit` 与 `queue copy` 共用同一段提交逻辑（`_submit_job`）。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/gui.py`（新） | `V2Panel`、`action_states`、`status_label`、`scope_choices`、命令调用 |
| `app/tools/upgrade/migrate.py`（新） | `migrate_job`、`read_legacy_task`、`build_legacy`、`legacy_summary` |
| `app/tools/upgrade/cli.py` | `revisions list`、`migrate` |
| `app/tools/one_click_roast.py` | 新增 `_build_v2_tab` 与一个选项卡（12 行），其余未改；新任务的剪映自动化默认不由 v2 面板提供，旧流程照旧（D16） |
| `tests/upgrade_gui_migrate_test.py`（新） | 7 个用例（见下） |
| `app/tools/upgrade/gui_queue.py`（新，第二部分） | `QueueTab`、新任务/选流/词典/资源/关窗对话框，列表文字与按钮规则等纯函数 |
| `app/tools/upgrade/gui.py`（第二部分） | 两个子选项卡、“打开原视频”、命令子进程可追踪（`start_cli`/`finish_cli`）、后台耗时、`confirm_close`、界面线程回调队列 |
| `app/tools/upgrade/runner.py`（第二部分） | `stop_flag_path`、`request_stop`、队列暂停与 `INTERRUPTED` 记法、`queue_rows` |
| `app/tools/upgrade/media.py`（第二部分） | `relocate_media` |
| `app/tools/upgrade/cli.py`（第二部分） | `queue stop/list/copy`、`media register/locate/relocate`、`resources list`；`_submit_job` |
| `app/tools/one_click_roast.py`（第二部分） | 保存面板引用，`_on_close` 先问面板（+5/−1 行） |
| `tests/upgrade_queue_gui_test.py`（新，第二部分） | 18 个用例（见下） |

## 验证

| 项（计划 W08 验收） | 状态 | 证据 |
| --- | --- | --- |
| 按钮只在规定条件可用 | PASS | `ButtonRules.test_buttons_are_usable_only_under_the_conditions_of_plan_3_2`；`PanelTests` 在真实数据根上核对两种格式未选时不能建副本 |
| 用户可不装编辑器而产字幕 | PASS | 机器版本和导出都不依赖编辑器（`PanelTests` 全程没有真实编辑器）；未配置编辑器时只打开文件夹 |
| 编辑器打开的是 session 副本 | PASS | `test_from_a_machine_version_to_a_confirmed_human_file_and_an_export`：启动参数正好是 `[编辑器, 副本路径]`，副本字节等于所选版本文件 |
| GUI 关闭不杀编辑器 | PASS（代码层） | 编辑器以分离进程启动、不保存句柄、不等待；真实编辑器进程的实测属 T08（W09） |
| 旧配置未重写 | PASS | 面板设置写到自己的文件；`test_the_old_window_only_gains_a_tab_…` 静态核对 |
| 看不到“模型完成 = 人工已审” | PASS | `test_no_label_says_reviewed_…`；真实数据根上：机器版本“等待人工检查”、登记后“部分确认（确认范围为空）”、确认 text+timing 后才“已人工确认” |
| 原文修订分支需要明确的第二步 | PASS | `test_a_corrected_japanese_source_needs_an_explicit_second_step_to_be_translated`：登记原文修订后没有任何新运行，直到点“据此生成新机器译文” |
| 显式导出版本 | PASS | 面板导出的 manifest 指向选中的人工版本，文件状态 REVIEWED |
| T05：旧任务副本迁移后旧 hash 不变 | PASS | `MigrationTests.test_an_old_task_becomes_a_new_project_and_its_directory_is_not_touched`：旧目录每个文件的哈希和修改时间迁移前后、再登记之后都不变；小文件逐字节复制、视频只引用；没有机器/人工版本被凭空生成；手改文件经明确登记为来源未审计的人工稿 |
| 非旧任务目录不建项目 | PASS | `test_a_directory_that_is_not_an_old_task_creates_nothing` |
| T08：真实 Subtitle Edit / Aegisub 往返、窗口与所选版本一致的人工验收 | NOT_RUN | 执行机没有安装编辑器（B05），属 W09 |
| 旧完整 GUI 候选保留（回滚） | PASS | 旧窗口所有选项卡与流程未改；新选项卡可整体移除 |
| 第 3.1 节新任务：来源与显示名必填、显示名只作一个参数 | PASS | `QueueLabelTests.test_a_new_task_needs_a_source_and_a_display_name`、`test_user_text_only_ever_becomes_one_argument`（含引号、`&`、`|` 的显示名原样成为一个参数） |
| 多音轨由用户选、词典不支持条目要确认、确认只问一次 | PASS | `PanelQueueTests.test_a_new_task_asks_for_the_audio_track_and_the_dictionary_entries_before_it_is_queued`：真实双音轨 MKV，命令先拒绝、未入队；选择后请求记录所选音轨，快照里有 1 个已接受条目；第二个任务不再询问 |
| 无音轨不悄悄改模式 | PASS | `test_a_file_without_sound_is_not_switched_to_another_mode`：提示改选“纯画面文字”，没有请求入队 |
| 已有日文源字幕的新任务 | PASS | `test_a_task_from_an_existing_japanese_subtitle_registers_video_and_source_first`：项目登记一个媒体，请求为 `supplied_source`，引用新源版本与其媒体 |
| 第 3.2 节队列列表与按钮条件 | PASS | `QueueLabelTests.test_queue_buttons_follow_plan_3_2`、`test_the_list_shows_the_stage_and_elapsed_time_never_an_invented_percentage`、`test_states_say_what_happened_and_what_can_be_resumed` |
| 停止当前 = 可恢复并暂停；跳过当前 = 取消并继续 | PASS | `StopSkipTests`（同进程执行器 + 自有子进程阻塞的阶段）：停止后该请求 `INTERRUPTED/queue_stop/ASR`、下一个仍 `QUEUED`；恢复后 ASR 新增一次 attempt，INPUT 不重跑；跳过后下一个完成；没有执行器时 `queue stop` 不写文件，遗留标记不阻止下一次开始 |
| 开始队列在窗口子进程中运行，结束后可恢复/复制/移除 | PASS | `test_the_queue_runs_as_a_child_of_the_window_and_its_buttons_follow_the_request`：真实 `queue run` 子进程（未登记识别引擎，如实 FAILED），列表与按钮随状态变化，恢复→排队中，复制→新排队请求，移除→“已移除（未开始）” |
| 复制为新任务采用当前配置、原请求不变 | PASS | `QueueCommandTests.test_copy_as_new_task_takes_the_configuration_as_it_is_now` |
| 打开原视频 / 重新定位媒体只接受同内容 | PASS | `test_media_is_opened_where_it_is_and_relocated_only_to_the_same_content`（命令）、`test_open_original_video_relocates_a_moved_file_with_the_same_content`（面板） |
| 资源重新定位 | PASS | `test_resources_list_shows_a_moved_tool_and_rebind_accepts_the_same_content` |
| 关窗只给“取消当前并退出 / 返回” | PASS | `test_closing_with_running_work_offers_only_stop_or_back`：选“返回”什么都不停；选退出后执行器和后台子进程都结束；没有运行的工作时不询问；旧窗口 `_on_close` 先问面板 |
| 界面实际外观 | PASS（截图） | 本机打开面板截图：队列选项卡（3 个请求，排队中/已移除）与新任务对话框两种模式，字段启用状态正确；截图只在执行机本地，不入库 |

全套：第一部分 旧 22 个 + 新 16 个测试文件，38/38 通过（2026-11-10T13:58Z UTC，证据 `LOCAL_WORKSPACE/roast-w08-tests-summary.json`）；第二部分 旧 22 个 + 新 17 个测试文件，39/39 通过（2026-11-11T03:36Z UTC，证据 `LOCAL_WORKSPACE/roast-w08b-tests-summary.json`）。均为 Windows、CPython 3.10.6 隔离 venv、真实 FFmpeg/ffprobe；新版测试没有用例被跳过，旧测试 `magia_utf8_runtime_test.py` 里需要真实 MagiaTimeline（`MAGIA_TEST_EXECUTABLE`）的 1 个用例照旧跳过（B01）。

第二部分实施中发现并修正的问题：

1. 关闭面板时一个定时器由错误的控件取消，Tk 报“can't delete Tcl command”；改为每个控件取消自己的定时器。
2. 有效数据根刷新后状态栏仍写“选择数据根后点刷新”；改为显示项目数和两个子选项卡的用途。
3. 测试本身：同一秒提交的两个请求按请求 ID 排序执行，测试一开始阻塞错了请求；改为按队列实际顺序取。

实施中由测试发现并修正的问题：

1. 新项目创建时已经有一个空的 `legacy/` 文件夹，迁移最初因此报"已存在"；改为空文件夹可以接管、非空才拒绝。
2. 迁移登记了媒体却没有写进项目的 `media_ids`；补上。

## 数据保护

旧任务目录只读，测试逐文件核对哈希与修改时间。旧配置文件不读写。测试只写临时目录。没有安装软件，没有访问外部网站。

## 未完成与限制

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 进度百分比 | 目前没有阶段提供可靠的 processed/total（计划 3.2 不允许估算），队列和导出只显示当前阶段与已耗时 | 以后某阶段能报告可靠进度时，`queue_rows.progress` 与列表文字已预留 |
| 窗口崩溃时执行器不随之结束 | 执行器是普通子进程，没有放进窗口的 Job Object；正常关窗会先停下它 | 崩溃后执行器跑完当前队列；下次启动按 10.2 处理 |
| 资源首次登记的界面 | 计划 3.4 的“工具/模型路径”目前只做重新定位；首次登记仍用命令行 `resources register` 或 `config migrate` | 需要时用命令行 |
| 新任务的 OCR 区域、字幕样式等设置界面 | 计划 3.4 的其余设置仍在配置文件里改；新任务取提交时的完整快照 | 改设置后用“复制为新任务”采用新配置 |
| 旧词典逐字节导入、旧配置迁移 | W01 的 `config migrate` 已处理旧配置；词典导入沿用 W04 | 未在迁移命令里再做一次 |
| 真实编辑器与人工验收 | B05 | T08 属 W09 |

## 回滚

撤回第二部分的提交即回到第一部分（db4fd04）：删除 `gui_queue.py` 与 `tests/upgrade_queue_gui_test.py`，撤回 `runner.py`、`media.py`、`cli.py`、`gui.py` 的改动与 `one_click_roast.py` 的 +5/−1 行；已经提交的请求、媒体绑定与版本都还能被第一部分读取（遗留的 `queue/stop.flag` 可直接删除）。再撤回第一部分即回到 W07（841b26d）：删除 `gui.py`、`migrate.py`、测试文件，撤回 `cli.py` 的两个命令与 `one_click_roast.py` 的 12 行。旧窗口与旧任务完全不受影响；迁移生成的新项目可整体删除，旧任务目录从未被写入。

## 下一包

W09 真引擎、编辑器和便携候选验收：需要本机有计划指定的引擎构建、Subtitle Edit / Aegisub 和干净的 Windows 环境（B01/B05/B09），本执行环境不具备，按实际情况登记。
