# completion-W01

```text
工作包：W01 运行时、配置、资源与存储基础；计划版本：1.1；基线提交：1a9a0104…（W00 台账提交 b12e2b2）
需求：FR12、FR14、FR15；决策：D11、D12、D17、D18 相关；验证：T01
```

## 实现

用户可观察的新行为（命令行；GUI 接入在 W08）：

| 行为 | 入口 |
| --- | --- |
| 显式选择并初始化 DATA_ROOT（写入探测成功才保存；网络盘、非法名、NUL 直接报错） | `python -m upgrade.cli config init --data-root DIR --config FILE` |
| 旧 `roast_pipeline.local.json` 迁移为 v2 `app.json` + `engine.json` + 迁移报告；旧 shell 模板只记录不执行，非内置模板报 `CONFIG_UNSUPPORTED_COMMAND`；未知旧键留在 `extensions.legacy_extra`；疑似凭据键被丢弃并在报告注明 | `config migrate --legacy FILE --data-root DIR --config FILE` |
| 用逻辑 ID 登记外部工具/模型并冻结内容身份（SHA-256 清单） | `resources register --config FILE --id ID --kind KIND --path PATH [--file REL]...` |
| 检查全部已登记资源：缺失报 `RESOURCE_MISSING` 并点名 ID 与路径；内容变化报 `RESOURCE_CHANGED`；只做版本/帮助探针，不启动模型；写出 CatalogReport | `resources inspect --config FILE --report FILE`（退出码 0/3） |
| 安装目录移动后只更新路径绑定（内容身份必须一致） | `resources rebind --config FILE --id ID --path PATH` |

修改/新增文件：

| 文件 | 内容 |
| --- | --- |
| `app/tools/upgrade/__init__.py` | 包声明 |
| `app/tools/upgrade/contracts.py` | 第 6 节全部记录类型（39 种）+ 资源声明文件 3 种的字段/枚举/不变量；`load_record`/`validate_record`/`require_record`/`dump_record`；统一错误对象与 12.1 退出码；规范 JSON 签名；安全相对路径与 Windows 名称检查；JSON Schema 导出 |
| `app/tools/upgrade/config.py` | AppConfig 默认值（第 6.2/6.8 节固定值）、读写、ConfigSnapshot（含 `config_sha256`，拒绝凭据键）、旧配置迁移（dialog_rect→roi 固定顺序） |
| `app/tools/upgrade/storage.py` | DATA_ROOT/项目目录布局、写入探测、网络盘拒绝、`safe_join`（realpath 越界检查）、原子小文件写、`.pending/` → 一次性 `rename` 的不可变提交原语、`verify_committed`（ARTIFACT_CHANGED） |
| `app/tools/upgrade/resource_catalog.py` | 资源声明读写、`inspect_resources`、`resolve_resource`、`require_resources`、`register_resource`、`rebind_resource`、大文件哈希缓存（size+mtime 加速，报告中说明边界）、版本/帮助探针、legacy `tool_discovery` 只用于建议 |
| `app/tools/upgrade/cli.py` | 上表命令；stdout 一份 OperationResult JSON，stderr 日志 |
| `schemas/*.schema.json`（50 文件 + index；原文误写为 43，W03 更正） | 由 `scripts/export_schemas.py` 从 contracts 导出；`--check` 可验证一致 |
| `tests/upgrade_*_test.py`（3 文件，29 用例） | T01 |
| `requirements-core.txt`、`requirements-legacy.txt`、`requirements-dev.txt`、`requirements.txt` | 依赖分组；PyYAML 显式声明；全量 `requirements.txt` 兼容旧文档 |
| `build/roast-onedir.spec` | one-dir、UPX 关闭、`roast.exe` + `roast-gui.exe` 共享 `_internal`；排除 torch/paddle/qwen；NOT_RUN |
| `docs/upgrade/runtime-lock.json`、`requirements-lock-dev-3.10.txt` | 3.13.17 选版记录与依赖锁状态 |

契约变化：无（第 6 节契约首次落地为代码）。未改 ASR/OCR/分段算法、旧 GUI、旧流水线、旧测试。

## 验证

| 项 | 状态 | 证据 |
| --- | --- | --- |
| T01 | PASS（3.10） | 3 个新测试文件；3.10 venv 全套 25/25；同 3 个文件在未装第三方包的系统 3.10 下 PASS |
| 缺少必需工具时出现具体错误、未启动模型 | PASS | `test_missing_resource_is_a_specific_problem_and_blocks_require`、CLI 测试退出码 3 + RESOURCE_MISSING + resource_id |
| 安装目录移动后资源可重新绑定 | PASS | `test_register_detect_change_and_rebind_after_move`（移动后 rebind 成功且身份不变；改内容后 RESOURCE_CHANGED） |
| 新 schema 拒绝未知版本 | PASS | `test_unknown_schema_version_is_refused`、声明文件 schema_version=1 被拒 |
| 用户配置未知旧键留档 | PASS | `test_legacy_migration_maps_known_values_and_records_the_rest`（`extensions.legacy_extra`） |
| 3.13 依赖导入/CLI 帮助可运行 | BLOCKED（B02） | 3.13 未安装；CLI `--help` 与依赖隔离导入只在 3.10 验证 |
| 旧 22 测试无回归 | PASS | 25/25（venv） |

证据文件：`LOCAL_WORKSPACE/roast-w01-tests-summary.json`、`test-results/`（不入库）。

## 数据保护

未修改任何旧业务文件（`git diff --stat` 只含新增文件与 `requirements.txt` 重写为分组引用）。测试只写临时目录与仓库内被忽略的 `test-results/`。未写用户文档目录、未触碰 D 盘资源。

## 未完成

| 事项 | 原因 | 影响范围 |
| --- | --- | --- |
| 安装 3.13.17 并生成带哈希的 3.13 依赖锁 | 需负责人授权下载安装（B02） | 冻结包、T09 |
| one-dir 构建实跑 | 依赖 3.13；3.10 下构建只能作为预查 | T09 |
| 真实二进制探针确认（ffmpeg/whisper-cli/llama-server 输出关键字） | 执行机没有 D06 默认引擎（B01） | W03/W09 |
| CLI 四条新增命令纳入 12.1 清单 | 变更单候选 CO-CAND-001 待负责人确认 | 文档 |

## 回滚

删除 `app/tools/upgrade/`、`schemas/`、三个新测试、`build/roast-onedir.spec`、依赖分组文件（恢复旧 `requirements.txt`）即回到 W00；旧入口 `scripts/run_gui.py`、`roast_pipeline.py` 未受影响。新 DATA_ROOT 内没有业务产物。

## 下一包

W02 持久队列、进程所有权和恢复（依赖 W01 已满足；T02 不需要模型）。
