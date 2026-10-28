# 项目文档索引

本目录按“当前说明 / 设计依据 / 实录音频证据 / 历史资料”分类。运行方式与
前端字段契约以根目录 [README.md](../README.md) 为准。

## 当前说明

| 文档 | 用途 |
|---|---|
| [../README.md](../README.md) | 快速开始、数据与模型准备、两次完整复述语义、逐段输出字段、服务协议、代码结构、已知限制 |
| [design/strict_verification_plan.md](design/strict_verification_plan.md) | 严格校验设计与 W1–W7 实施记录。§1–§10 为历史设计，**现行语义以 §11 为准** |
| [design/kir_schema_and_extraction.md](design/kir_schema_and_extraction.md) | KIR Schema、抽取语法、字段比较与判定规则；文首标注了与现行实现的差异 |

## 实录音频证据

| 文件 | 内容 |
|---|---|
| [evidence/w7_twice_confirmation_report.json](evidence/w7_twice_confirmation_report.json) | 小留 ticket1（35 段，`required_confirmations: 2`）的条目级终态报告：四态、提醒、逐条次数与重读记录 |

## 设计依据

| 文件 | 内容 |
|---|---|
| [corpus_analysis/report.md](corpus_analysis/report.md) | 155 个操作票 JSON、107 张去重票、944 条唯一条目的语料分析报告。只读资料，用于解释 `configs/kir.yaml` 词表与抽取规则的来源 |

## 历史资料

| 文件 | 说明 |
|---|---|
| [倒闸操作唱票复述校验系统技术介绍_更新版.docx](倒闸操作唱票复述校验系统技术介绍_更新版.docx) | 2026-09 的技术介绍，描述的是“槽位匹配 + 分数排序”旧方案。W7 已改为 KIR 逐字段严格比较与两次完整复述；引用前请以根 README 和 `strict_verification_plan.md` §11 为准 |

## 清理说明

2026-10-28 项目整理时删除了以下不再使用的产物，避免与现行实现混淆：

- W4/W5 阶段的逐段评测 JSONL、延迟记录、麦克风路径记录（`docs/kir_export/`）；
- 一次性语料分析脚本与统计中间件（`docs/corpus_analysis/analyze_corpus.py`、`stats.json`），保留可读的 `report.md`；
- 旧版技术介绍 docx 与两幅描述“槽位匹配/单次判定”的架构图；
- 未引用的 `configs/stations/dandong_assets.txt`、根目录测试报告 `demo_report.json`、Python/pytest 缓存。

当前只保留可复现的运行代码、KIR 词表与配置、单测、设计文档、语料分析报告和
W7 实录音频报告。
