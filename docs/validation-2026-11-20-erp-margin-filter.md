# ERP 修正一次毛利筛选清理与本地核验（2026-11-20）

## 当前行为

ERP 下载财务表自带「修正一次毛利」。负金额进入亏损候选，零或正数不作为亏损；缺失、非有限值或多来源冲突保持异常，不填零、不重算替代。金额按已确认币种、数量和同 SKU 归属转换为单件亏损。

已清理额外的独立现价毛利率计算、非负排除分支、在线快照字段、来源映射、模拟数据及相关注释和文档。完整在线费用模型继续包含营销费、VAT 和基础平台费用；补亏候选价、进档价、人工修正价及回读价仍执行统一至少 1% 的完整在线利润率校验。72 小时官方日志保护和人工保护保留。

规则版本为 `loss-erp-corrected-margin-1pct-20261120-v9`。文件补充合同采用 `erp-pricing-evidence-v2`，实时在线准备采用 `erp-live-online-preparation-v2` / `erp-live-online-v2`；旧补充文件需重新生成，不提供兼容分支。ERP 原始下载、历史已保存判定和核验档案不回写。迁移细节见 [ERP 对账说明](erp-pricing-preview.md)。

## 验证

- 在 WSL2 / Python 3.12 执行；新增直发、海外仓两项集成回归在修改前均复现额外拦截，修改后均通过。手算 ERP 亏损 1 CNY、报价 0.1 CNY/PHP、现价 100 PHP 时，补亏实际价 110 PHP，五折上传价 220 PHP。
- 新增 ERP 原始字段的负数、零、正数、缺失四项回归；即使其他毛利字段符号相反，候选仍只按「修正一次毛利」选取。新增共 6 项全部通过。
- `cd frontend && npm run build`：通过。
- `.venv/bin/python -m pytest -q`：864 项，845 passed、19 failed，用时 500.39 秒；1 条 Starlette/AnyIO 依赖弃用警告。全量未全绿。
- 将 Git `bf0d721` 源码导出至独立临时目录，分别复跑下列 2 + 17 个失败用例，19 项全部仍失败，失败用例集合与本次全量一致。临时源码已自动清理。本次没有修改这些既有失败用例的业务预期。
- 独立代码复核确认完整模型费用系数、VAT、固定成本及汇率方向保持一致；`git diff --check` 通过。

原始测试日志位于本机 `runtime/validation-erp-margin-20261120/`：`pytest.log`、`baseline-first-failures.log`、`baseline-remaining-failures.log`。

## 已在修改前版本复现的失败用例

- `tests/test_erp_export_mapping.py::test_raw_and_approved_evidence_cannot_silently_disagree[quantity-evidence_quantity_mismatch]`
- `tests/test_erp_export_mapping.py::test_exports_can_be_uploaded_and_reopened_by_an_admin`
- `tests/test_official_price_logs.py::test_successful_reconcile_saves_structured_price_record`
- `tests/test_official_price_logs.py::test_manual_lookup_collects_official_logs_and_returns_four_prices`
- `tests/test_online_profit_only.py::test_automatic_price_uses_only_full_online_profit`
- `tests/test_online_profit_only.py::test_manual_price_uses_only_full_online_profit`
- `tests/test_review_regressions.py::test_sku_rename_cannot_bypass_unknown_or_verified_cooldown`
- `tests/test_review_regressions.py::test_midnight_stops_cached_submissions_and_queues_fresh_day[False]`
- `tests/test_review_regressions.py::test_midnight_stops_cached_submissions_and_queues_fresh_day[True]`
- `tests/test_review_regressions.py::test_current_items_and_counts_omit_resolved_history_but_preserve_batches`
- `tests/test_review_regressions.py::test_current_items_keep_pending_execution_even_after_newer_blocked_record`
- `tests/test_review_regressions.py::test_migration_preserves_legacy_verified_cooldown_and_json_history`
- `tests/test_review_regressions.py::test_migration_keeps_all_legacy_pending_until_each_is_reconciled`
- `tests/test_review_regressions.py::test_migration_uses_latest_guard_time_and_keeps_it_after_pending_rejection`
- `tests/test_review_regressions.py::test_execution_dependent_proposal_follows_terminal_outcome_without_rewriting_history[verified]`
- `tests/test_review_regressions.py::test_execution_dependent_proposal_follows_terminal_outcome_without_rewriting_history[failed]`
- `tests/test_review_regressions.py::test_terminal_dependency_does_not_hide_newer_independent_mapping_exception`
- `tests/test_review_regressions.py::test_current_projection_keeps_multiple_pending_dependencies_until_all_resolve`
- `tests/test_review_regressions.py::test_old_terminal_result_does_not_replace_new_external_pending_source`

本次为本地代码、合同及说明清理，未部署、重启真实服务、提交真实价格或发送通知。
