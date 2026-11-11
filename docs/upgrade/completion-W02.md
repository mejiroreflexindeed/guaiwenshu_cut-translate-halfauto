# completion-W02

```text
工作包：W02 持久队列、进程所有权和恢复；计划版本：1.1；基线提交：1a9a0104…（W01 提交 1d7e2fb）
需求：FR09、FR15、FR06；决策：D11、D19 相关；验证：T02
```

## 实现

用户可观察的新行为（命令行；GUI 接入在 W08）：

| 行为 | 入口 |
| --- | --- |
| 提交任务请求：先持久化到 `queue/requests/<request_id>.json` 再报告"已加入队列"；同 request_id 重复提交返回原请求、不再运行；不自动开始推理 | `submit --config FILE --video FILE|--url URL --mode MODE [--timing-mode] [--project] [--video-stream] [--audio-stream N|none] [--copy-original] [--accept-dictionary-warnings]` |
| 执行队列：取得 DATA_ROOT 执行锁（OS 字节锁），第二个执行器立即得到 EXECUTOR_BUSY（退出码 7）；启动先恢复；按 created_at、request_id 顺序串行执行；队列耗尽即退出 | `queue run --data-root DIR [--stop-on-error]` |
| 取消：未开始请求直接 CANCELLED（JSON 保留）；运行中的请求通过标记文件请求执行器停止，只终止自有子进程 | `queue cancel --data-root DIR --request ID` |
| 恢复：失败/中断的运行排回队列，保留原配置，新增 attempt，已提交阶段不重跑 | `resume --data-root DIR --run ID` |
| 旧收件箱接管：pending 与崩溃遗留的 working 文件入库，内容哈希去重，入库成功后才移到 `taken-over/` | `inbox intake --config FILE --inbox DIR` |

机制（计划第 10 节）：

- **阶段引擎**：8 个 v2 阶段按模式表 SKIPPED（MODE / USER_SOURCE）；每个 attempt 写入 `stages/<stage>/.pending/<id>/`，哈希后写 StageManifest，再 `rename` 到 `<attempt_id>/`，之后才更新 task.json 指针；失败/取消的 attempt 留下 `.pending` 候选，不变 DONE。
- **缓存签名**：`stage_signature = sha256(规范JSON{stage, stage_version, request_fingerprint, 实现给出的 inputs})`；命中需要签名相同且 manifest 所列文件全部重新校验通过；可跨同项目的历史 run 复用。参数/算法变化使该阶段及下游重算。
- **恢复**：启动扫描只在拿到执行锁时进行（PID 不作依据）；RUNNING→INTERRUPTED；从已提交 manifest 重建丢失的指针并校验 run_id/request_id/stage 一致。
- **进程所有权**：`run_process(ProcessSpec, cancel, event_sink)`：argv、shell=False、显式 cwd、隐藏控制台、Windows Job Object（KILL_ON_JOB_CLOSE）；私有环境只记录被覆盖的变量名；取消顺序 = 退出请求 → 3 秒 → 终止该作业；日志 stdout/stderr/process.json 在 attempt 目录内。
- **锁顺序**：数据根执行锁 → 项目提交锁（project.json 写入用短锁）。

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/process_runner.py` | CancelToken、ProcessHandle、`run_process`、`process_spec` |
| `app/tools/upgrade/locks.py` | ByteLock（msvcrt.locking）、ExecutorLock（owner JSON 仅展示）、ProjectCommitLock、`executor_is_live` |
| `app/tools/upgrade/runner.py` | `build_job_request`、`submit`、`cancel`、`resume`、`recover`、`recover_run_pointers`、QueueRunner（run_queue / 阶段引擎 / 缓存 / 失效 / 取消）、项目记录读写 |
| `app/tools/upgrade/inbox.py` | 旧 inbox 接管（复用 `roast_inbox.parse_request` 纯函数） |
| `app/tools/upgrade/stage_registry.py` | 阶段实现注册表（W02 为空） |
| `app/tools/upgrade/cli.py` | 新增 submit / queue run / queue cancel / resume / inbox intake；`config init --config` 可选 |
| `app/tools/upgrade/contracts.py` | 错误码 STAGE_FAILED、STAGE_UNAVAILABLE |
| `tests/upgrade_process_runner_test.py`、`tests/upgrade_runner_test.py` | T02（20 用例） |

契约变化：无字段变化；错误码集合扩展两项（IF-05）。未改旧 GUI、旧流水线、旧 TaskState、旧测试；旧 `roast_inbox.py` 仍可独立运行。

## 验证

| 项（计划 W02 验收） | 状态 | 证据 |
| --- | --- | --- |
| T02 | PASS | 2 个测试文件 20 用例；全套 27/27（3.10 venv） |
| 双执行器只有一个能写 | PASS | `test_second_executor_is_refused`、`test_second_executor_process_gets_exit_code_7` |
| 入队后崩溃不丢未开始项 | PASS | 入队即落盘；`test_cancel_queued_keeps_json_and_is_not_run`、恢复测试中 QUEUED 项在新执行器中被处理 |
| 中断后不重复接收 inbox | PASS | `test_intake_pending_working_and_duplicates`（映射存在且 envelope 存在 → duplicate） |
| 取消不杀无关进程 | PASS | `test_cancel_terminates_owned_tree_and_spares_unrelated_process`、`test_cancel_running_request_kills_owned_child_and_leaves_no_done` |
| 失败/半产物不变 DONE | PASS | `test_failure_keeps_candidate_and_resume_adds_attempt`、`test_crash_inside_stage_is_a_failed_attempt_not_done` |
| 相同 request_id 幂等 | PASS | `test_submit_persists_before_report_and_is_idempotent` |
| 人工文件 hash 不变 | PASS | `test_committed_review_bytes_survive_runs_and_cancel` |

证据文件：`LOCAL_WORKSPACE/roast-w02-tests-summary.json`。

## 数据保护

未修改任何旧业务文件。测试只写临时目录。无用户目录写入，未触碰 D 盘资源。

## 未完成

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 真实阶段实现 | 属 W03/W04/W06 | `queue run` 对真实请求当前以 STAGE_UNAVAILABLE 失败（诚实状态） |
| 优雅退出请求对无控制台引擎无效 | Windows CTRL_BREAK 需要控制台；按计划 3 秒后终止作业 | 引擎接入时可按各引擎支持方式扩展退出请求（变更单） |
| GUI 接新 Interface | W08 | — |
| CLI 新增命令纳入 12.1 清单 | CO-CAND-001/002 待负责人确认 | 文档 |

## 回滚

删除 `process_runner.py`、`locks.py`、`runner.py`、`inbox.py`、`stage_registry.py`、两个测试文件，并撤回 `cli.py`/`contracts.py` 的 W02 片段即回到 W01（1d7e2fb）。v2 DATA_ROOT 中的队列文件可整体删除，不影响旧 `app/work` 任务。

## 下一包

W03 媒体坐标与两类识别接入（依赖 W02 已满足）。执行机缺 ffprobe/whisper-cli/Magia（B01），W03 的真实二进制路径只能在资源到位后验证；契约、时钟计算与 fixture 解析可先实施。
