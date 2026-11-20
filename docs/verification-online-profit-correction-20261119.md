# 在线利润校正额命名与明细验证（2026-11-19）

统一业务名称为“在线利润校正额”，含义为：初步补亏价通过库存对应完整在线利润校验还需增加的实际价金额，单位为站点币/件；已通过校验则为0。不含后续上传进档及平台回读差异。

## 变更

- `engine.py`保留原价格算法，计算并保存`trace.online_profit.correction_amount`。
- `OnlineProfitCheck.minimum_actual_price()`替代含糊的内部方法名；该方法只提供校验下限，未新增门槛或兼容层。
- 商品计算详情展示“在线利润校正额（站点币）”；未保存校正额的记录显示“未记录”，不回写历史或在前端重算。
- 设计、计算规范、README及三张Archify图同步术语。原始证据中的最低通过价继续作为计算依据保存，不作为另一道一次毛利门槛。
- 未发现旧截图的独立一次毛利保本计算路径。新目标价仍只使用库存对应的完整在线利润模型。

## 验证

- 新增两项金额验证：需要校正时11.2变成12.5，校正额1.3；无需校正时金额为0。均验证五折只影响上传价，不让校正额翻倍。先见缺失字段失败，再实现通过。
- 计算相关测试：93 passed。
- 前端：`npm run build`通过；records-model Node测试6 passed。
- 桂露原始历史Snapshot分别用修改前ace15be和当前代码离线计算：全部原有Decision字段逐项一致，仅新增correction_amount。
- 三张HTML的validate、deliver、check、browser-check均通过；本次未作人工视觉验收，保留原有布局。
- `git diff --check`通过。
- 全量测试：815 passed、15 failed、1 warning，512.06秒。warning为Starlette引用AnyIO旧别名的DeprecationWarning。
- ERP导出两个失败已在未修改的ace15be基线复现（24 passed、2 failed）。另13项回归测试失败也已在同一未修改基线复现（7 passed、13 failed），均为演示样本数量/调用数预期4、实际5；全量15项失败均已确认在修改前存在。

## 全量失败清单

- `tests/test_erp_export_mapping.py::test_raw_and_approved_evidence_cannot_silently_disagree[quantity-evidence_quantity_mismatch]`
- `tests/test_erp_export_mapping.py::test_exports_can_be_uploaded_and_reopened_by_an_admin`
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

这些失败未在本次命名任务中改动业务逻辑或放宽断言。

## 交付边界

仅本地工作区和HTML更新。未提交业务仓库、未部署服务器、未调用真实调价或通知。原有7项未提交日志页面及测试改动保留，其内容不归于此次命名变更。
