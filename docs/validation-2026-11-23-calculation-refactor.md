# 亏损提价计算层重构验证

2026-11-23 在 WSL 的 Python 3.12 环境验证。对照版本为接手续改时的提交 `e92d352`，重构前后使用同一套 Python 依赖、合成输入及临时测试数据库。

## 计算与来源

- 自动提价和人工修正都从在线快照取得当前实际价和精确折扣，按该折扣反推含税上传价并复核进档后的实际价。无营销价时按零售价成交，显式零价或营销价高于零售价按现有原因退出。
- 费用计算层负责直发每 10 克运费进档、含 VAT 的 CNY 固定成本及完整在线利润系数。销售适配层核对库存、仓位、采购和头程归属、费率分项合计，再保存费用来源。
- 快照里的原始报价单位决定换汇方向。同币种使用系数 1，也检查当日报价有效性。自动路径按原顺序检查汇率和现价，人工修正继续校验明确指定的价格。
- 利润模型集中求解 1% 所需实际价。存在有限下限时保留精确比值，无法达到时返回空值。有同 SKU 参考时，展示与限价共用三条样本的精确均价，上限仍优先。无参考时沿用原有的模型有效性和 1% 校验。自动公式版本为 `loss-erp-corrected-margin-reference-priority-20261120-v13`。
- 费率字段组装共用一处。实时入口校验本轮来源合同，直发按店铺取公共费率，显式导入的固定表按站点读取。泰国直发 VAT 取自当前父商品。部门接口和 11 月费率表的对应规则仍待确认，代码保留了该项待办。

## 重构前后逐字段对照

对基线和工作区分别运行同一份离线脚本。按键排序后比较完整 JSON 行，包含价格、判定状态、原因、精确分子分母、计算过程及费用来源。对无效输入比较异常类型和信息。

| 对照范围 | 组数 | 覆盖输入 |
| --- | ---: | --- |
| 自动算价 | 3,240 | 两种汇率方向、三种亏损粒度、三种舍入、折扣、利润系数与参考上限组合 |
| 人工指定价 | 540 | 明确授权价格在利润线或参考上限两侧，以及舍入后的实际价 |
| 直发费用 | 24 | 首重、超重、两档边界、VAT 与公司原始报价 |
| 海外仓费用 | 16 | 成本归属、分项合计、仓配错误及来源记录 |
| 费率选择 | 72 | 实时与固定来源、按店或站点、部分采集、缺失字段及泰国类目 VAT |

3,892 组输出逐字节相同。规范快照 `Snapshot` 和算价判定 `Decision` 的 JSON Schema 与基线相同。离线对照脚本与输出摘要保存在本机验证目录。[对照脚本](../runtime/validation-calculation-refactor-20261123/compare_pricing.py) · [组数与 SHA-256](../runtime/validation-calculation-refactor-20261123/comparison.json)

一条 MY 手算样本使用现营销价 10、含税零售价 20、单件补亏 1.2 和利润公式 `0.8 × 营销价 − 10`。无参考时上传 25.32，对应营销价 12.66、利润 0.128。参考上限为 12.50 时上传 25.00，利润为 0。参考上限为 9.00 时上传 18.00，利润为 −2.80。两版的上传价及利润逐项相符。

## 专项回归

定价、公式、参考上限、费率、在线准备与人工修正共 284 项通过。历史保护、告警、官方日志计数及用户规则另有 38 项通过。新增的计算层测试覆盖同币种缺报价、折扣零值、循环小数及利润模型没有有限下限等边界。[算价专项日志](../runtime/validation-calculation-refactor-20261123/targeted.log) · [保护专项日志](../runtime/validation-calculation-refactor-20261123/protection.log)

Cursor 报错中的 `tests/test_corrections.py` 路径不存在。专项测试通过仓库实际的人工修正入口覆盖指定价、参考上限与协作执行。[利润率测试](../tests/test_one_percent_margin.py) · [参考上限测试](../tests/test_sku_price_reference.py) · [协作执行测试](../tests/test_coordination_execution.py)

## 后端全量回归

先完成前端构建，再在干净基线和当前工作区分别运行 `.venv/bin/python -m pytest -q --tb=short`。两个进程使用独立的临时目录。

| 版本 | 通过 | 失败 | 弃用提示 | 耗时 |
| --- | ---: | ---: | ---: | ---: |
| 基线 `e92d352` | 926 | 17 | 1 | 994.80 秒 |
| 本次工作区 | 951 | 17 | 1 | 952.75 秒 |

逐项比较 pytest 用例 ID，17 项失败完全一致。新增 25 个计算层用例全部通过。两版的弃用提示均来自 Starlette TestClient 使用的 AnyIO 别名。[基线全量日志](../runtime/validation-calculation-refactor-20261123/baseline.log) · [工作区全量日志](../runtime/validation-calculation-refactor-20261123/current.log)

| 遗留用例 | 数量 | 实际失败原因 |
| --- | ---: | --- |
| 执行、历史与迁移 | 13 | 断言沿用演示数据的 4 条提交或相关记录，当前演示数据产生 7 条。 |
| 自动和人工在线利润 | 2 | 旧用例要求无参考时以 12.50 放行，此价利润为零。现行 1% 规则下该样本的最低实际价为 10 ÷ 0.79，约为 12.65823。 |
| 官方价格日志回读 | 2 | 持久请求样本缺少 `trace.online_rows`，人工回读样本缺少 `company_parent`。在各自的核验入口捕获到对应的 KeyError，两条模拟记录因此保留 `unknown`。 |

以下为两版共同的失败用例。具体回读缺字段的位置也已在合成测试中复查。[回读诊断日志](../runtime/validation-calculation-refactor-20261123/readback-diagnosis.log)

- `tests/test_official_price_logs.py::test_manual_lookup_collects_official_logs_and_returns_four_prices`
- `tests/test_official_price_logs.py::test_successful_reconcile_saves_structured_price_record`
- `tests/test_online_profit_only.py::test_automatic_price_uses_only_full_online_profit`
- `tests/test_online_profit_only.py::test_manual_price_uses_only_full_online_profit`
- `tests/test_review_regressions.py::test_current_items_and_counts_omit_resolved_history_but_preserve_batches`
- `tests/test_review_regressions.py::test_current_items_keep_pending_execution_even_after_newer_blocked_record`
- `tests/test_review_regressions.py::test_current_projection_keeps_multiple_pending_dependencies_until_all_resolve`
- `tests/test_review_regressions.py::test_execution_dependent_proposal_follows_terminal_outcome_without_rewriting_history[failed]`
- `tests/test_review_regressions.py::test_execution_dependent_proposal_follows_terminal_outcome_without_rewriting_history[verified]`
- `tests/test_review_regressions.py::test_midnight_stops_cached_submissions_and_queues_fresh_day[False]`
- `tests/test_review_regressions.py::test_midnight_stops_cached_submissions_and_queues_fresh_day[True]`
- `tests/test_review_regressions.py::test_migration_keeps_all_legacy_pending_until_each_is_reconciled`
- `tests/test_review_regressions.py::test_migration_preserves_legacy_verified_cooldown_and_json_history`
- `tests/test_review_regressions.py::test_migration_uses_latest_guard_time_and_keeps_it_after_pending_rejection`
- `tests/test_review_regressions.py::test_old_terminal_result_does_not_replace_new_external_pending_source`
- `tests/test_review_regressions.py::test_sku_rename_cannot_bypass_unknown_or_verified_cooldown`
- `tests/test_review_regressions.py::test_terminal_dependency_does_not_hide_newer_independent_mapping_exception`

## 前端与交付范围

北京时间 11:57 对工作区前端执行生产构建和 Node 测试，本次检查开始及结束时源码摘要相同。构建通过，页面测试 28 项通过、4 项失败。基线提交的前端为 23 项全部通过。工作区同时另有提价日志界面与测试的编辑，本次计算重构只修改了后端源码、计算层测试及说明。[前端构建日志](../runtime/validation-calculation-refactor-20261123/front-build.log) · [前端测试日志](../runtime/validation-calculation-refactor-20261123/front-tests.log) · [基线前端日志](../runtime/validation-calculation-refactor-20261123/base-frontend-tests.log)

- `dialog stages use wrapping horizontal fields with saved price and original margin`
- `all log statuses occupy a centered top row above the upload label and amount without overlap`
- `missing upload and ERP margin evidence are not replaced by other saved values`
- `missing concrete item IDs stay optional and never use parent IDs or SKU as replacements`

本次交付为 WSL 本地工作区改动。此次运行使用本地数据和模拟适配器，未部署、未提交真实价格、未发送业务通知。公司费用模型与实际生效价的逐站点核验仍按项目接入边界处理。
