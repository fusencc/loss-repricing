# 2026-11-20 条件改为标识及钉钉告警

用户确认：近72小时历史无法确认、商品不可售、合同或费用模型无效不再单独归入待处理，告警推送到常福生钉钉。

当前行为：
- 历史未知保留未知事实，候选采集、常规算价、人工修正和提交前复查不再以此拦截；不伪造零记录。
- 商品不可售保留告警标识，继续按有效数据算价。
- 站点合同、适用仓配或完整利润模型缺有效计算依据时，不调价并告警，不补造参数，不生成不可验证价格。
- 已知近72小时调价记录、人工保护、未完成提交和未知执行锁继续有效。
- 商品证据记录 warning_codes 和 warnings，商品详情显示告警；新增提交前告警同步持久化到商品记录。
- 管理员告警与销售员处理通知独立，每批去重，发送结果未知不重传；LR_DING_ALERT_USERID 绑定本机已核验人员目录中的常福生，不把收件人ID写进源码。

本地验证：前端 npm run build 通过；算价、采集、公式测试123项通过；执行保护相关测试10项通过；通知告警及未知发送测试2项通过。全量测试命令：`.venv/bin/python -m pytest -q --tb=short --basetemp=/dev/shm/lr-warning-policy-20261120-final2`。结果861通过、17失败、1条依赖弃用告警，耗时32.33秒；完整输出为本机 `runtime/warning-policy-pytest-final.txt`。临时SQLite等测试数据放在隔离内存目录，避免WSL磁盘fsync拖慢。

真实通道：已向常福生发送明确标注的告警通道测试，官方回执 delivered（送达），证据 runtime/warning-policy-dingtalk-check.json。本机通知守护已启动，健康状态为 running；没有提交真实价格，没有部署云端业务服务，没有回写历史判定。

## 剩余失败与对照

在独立测试进程内仅恢复“历史未知则不放行”的旧判定，再运行对应三个测试文件，相同17项全部复现（17失败、16通过），没有改动工作区业务代码。此对照能排除本次历史未知放行是这些失败的必要原因，不等同于完整回滚到任务开始前的所有代码。结果存于 `runtime/warning-policy-prior-check.txt`。本次未扩展修改这些旧测试或它们覆盖的业务实现。

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
