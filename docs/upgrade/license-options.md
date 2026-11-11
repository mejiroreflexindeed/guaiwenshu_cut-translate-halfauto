# 主项目许可证选择材料（W00 交负责人决定，B10）

状态：**待负责人决定**。本文件只整理有依据的可选方案和每个方案要核对的事实，不替权利人选择，不创建 LICENSE 文件（计划 1.1 第 19.1 节、B10）。

## 1. 已经确定的前提

- 用户已明确：源码会在 GitHub 开源，维护者自己不作商业用途（CHANGE-001）。
- "自己不商用"是用途说明，不等于要在许可证里禁止下游商用。标准开源定义不允许一概禁止商业用途（OSI 开源定义第 6 项）。若负责人要禁止他人商用，应另选"带用途限制的源码公开"方案，不能给 MIT/GPL 等标准许可证附加矛盾条款。
- 首版仓库和可分发程序包都不捆绑模型、第三方工具二进制、字体和用户素材（D18、第 19.2 节），所以外部引擎的许可证不直接决定主项目源码的许可证。

## 2. W00 对拟公开代码来源的核对结果

详见 `source-origin-inventory.json`。摘要：

| 范围 | 核对方法 | 结果 |
| --- | --- | --- |
| `app/tools/*.py` 23 个模块、`tests/*.py` 22 个脚本、`scripts/`、`build/` | 全文检索版权声明、许可证字样、"adapted/ported/based on"、外链；对照 `docs/publication.md` 和审计报告 01 对源码来源的记录 | 未发现第三方版权头或复制声明；审计记录为维护者自有开发源码。标为 `self_authored`，但需负责人确认"没有从 VoiceTransl、GalTransl、MagiaTimeline 或其他项目复制/改写过代码片段" |
| `app/config/dict_*.txt`、`translation_glossary.json`、`translation_style_prompt.txt` | 逐文件阅读 | 内容是本项目的示例词条和风格要求；格式沿用 GalTransl/VoiceTransl 的 native 词典语法（格式兼容不构成复制受保护内容），需负责人确认词条不是从他人词典文件复制 |
| `docs/` | 阅读 | 维护者/审计 AI 撰写 |
| 第三方 Python 依赖 | 见下表 | 不进入源码仓库，只以版本锁出现；冻结包实际包含时在 W10 的 THIRD_PARTY_NOTICES 列出 |

## 3. 运行时和依赖的许可证事实（影响冻结包，不影响源码仓库）

| 组件 | 许可证 | 与主项目许可证的关系 |
| --- | --- | --- |
| CPython 3.x、Tcl/Tk | PSF-2.0、BSD 类 | 宽松，与任何常见开源许可证兼容 |
| PyInstaller 6.x | GPL-2.0-or-later，带 bootloader 例外 | 例外允许生成的程序使用任意许可证；冻结包需保留其通知 |
| PyYAML | MIT | 宽松 |
| Pillow | MIT-CMU（HPND 系） | 宽松 |
| yt-dlp | Unlicense | 公有领域式 |
| imageio-ffmpeg | BSD-2-Clause；安装时自带的 FFmpeg 二进制按其构建为 LGPL/GPL | 二进制不进仓库；冻结包是否收入需在 W01/W10 单独决定并处理通知 |
| pywinauto | BSD-3-Clause | 宽松（legacy 组） |
| pywin32 | PSF-2.0 | 宽松（legacy 组） |
| comtypes、six | MIT | 宽松（pywinauto 传递依赖） |

外部引擎（只通过进程/HTTP 调用，不复制源码、不捆绑）：VoiceTransl v1.21 为 GPLv3 文本且 README 有非商业声明疑问；FFmpeg 现有构建为 GPL；llama.cpp 为 MIT；MagiaTimeline 按其仓库许可证；Sakura 模型卡为 CC BY-NC-SA 4.0。按第 19.3 节，进程边界调用不自动触发复制源码的义务，但也不自动免除实际复制部分的义务。

## 4. 可选方案

| 方案 | 适合的情况 | 需要负责人确认的事实 | 实施影响 |
| --- | --- | --- | --- |
| **MIT** | 希望任何人可自由使用、修改、再分发，包括商用；只要求保留版权声明 | 第 2 节两项"自有来源"确认为真 | 最简单；对下游没有共享源码要求 |
| **Apache-2.0** | 同 MIT 的自由度，另有明确的专利授权和贡献条款 | 同上 | 需 NOTICE 文件惯例；与 GPLv2 代码不兼容，但本项目未复制 GPL 代码 |
| **GPL-3.0-or-later** | 希望下游修改版也必须开源 | 同上；并确认接受下游必须以 GPL 分发衍生品 | 不禁止商用；对将来"整合包捆绑 GPL 构建的 FFmpeg"最少摩擦 |
| **MPL-2.0** | 文件级 copyleft：改了的文件要公开，可与专有代码组合 | 同上 | 介于前两类之间 |
| 非 OSI 的"源码公开、禁止商用"条款（如 PolyForm Noncommercial） | 负责人明确要求禁止他人商用 | 这不是"开源"；README 不能再称开源发布；需负责人明确要求 | 与 CHANGE-001"开源"表述冲突，选此项需先改口径 |

## 5. 负责人需要回答的三个问题

1. 第 2 节的来源确认：`app/tools`、`tests`、`app/config` 是否都是自己（含 AI 协助）编写，没有复制或改写他人代码/词典？若有，请列出文件和来源，W00 会把它们改登记为 `third_party_copied/modified` 并单独处理通知。
2. 是否允许他人商用？允许→从 MIT / Apache-2.0 / GPL-3.0 / MPL-2.0 中选；不允许→第 4 节最后一行，并修改"开源"口径。
3. 是否希望下游修改版必须开源？是→GPL-3.0-or-later 或 MPL-2.0；否→MIT 或 Apache-2.0。

回答后，W10 才生成 `LICENSE`、`THIRD_PARTY_NOTICES.md`；在此之前 `validation-ledger.json` 中 B10 保持 BLOCKED。

## 6. 外部依据

- OSI 开源定义：https://opensource.org/osd
- GitHub 无许可证说明：https://choosealicense.com/no-permission/
- PyInstaller 许可证与例外：https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt
- Sakura 模型卡：https://huggingface.co/SakuraLLM/Sakura-GalTransl-7B-v3.7
- VoiceTransl 固定版 LICENSE：https://github.com/shinnpuru/VoiceTransl/blob/788a25b59e8f8c0d5556ab47c18a4a9d17e3931f/LICENSE
- FFmpeg n7.1 许可说明：https://github.com/FFmpeg/FFmpeg/blob/n7.1/LICENSE.md
