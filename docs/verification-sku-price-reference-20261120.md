# 同 SKU 营销参考价验证（2026-11-20）

本记录对应本日第一轮「仅展示参考」实现与验证；用户随后确认参考作为价格上限，当前行为以[上限设计](superpowers/specs/2026-11-20-sku-price-reference-design.md)为准。以下测试结果为前一轮记录，不冒充上限阶段验收。

已按本会话确认规则实现可选参考：同站点、同本地子 SKU、同当前仓配，排除本父商品链接，按子规格近七天正销量取三条其他链接。每条用含 VAT 税后价×（100－折扣）÷100，取算术平均。明确无折扣取0；参考缺失/不足三条/销量缺失或为0/查询失败均省略参考，不新增待处理或告警，不改变建议价及现行1%完整在线利润下限。

普通ERP编排与人工单商品采集复用同一采集器，参考独立归档、绑定在线快照并进入判定trace，商品详情与销售员日志展示保存均价及三条明细。没有新数据库表/迁移、旧接口回退或生产价格上限。

## 验证结果

- 核心定价、参考采集、准备和人工入口专项：78 passed。
- 相关工作流、记录API与协同回归：129 passed（与专项存在重叠，不相加）。
- 前端 `npm run build`：通过；新参考组件SSR两个用例通过，验证有参考显示、无参考隐藏及不在前端重算金额。
- 前端全部Node测试：12 passed、1 failed；失败是现有状态文案测试期待「活动中」，当前代码已统一「待处理」。未修改的HEAD 98c129d对应记录模型测试同样8 passed、同1 failed。
- 后端全量：893 passed、17 failed、1 warning，635.30秒。17项在未修改的HEAD 98c129d临时目录逐项重跑全部复现；没有未复现的失败。warning为既有Starlette/AnyIO弃用提示。
- `git diff --check`：通过。
- 已保存六条真实响应离线回放：两条有Top3生成精确均价，其余四条省略；仅为离线回放，不宣称当前全平台排名。
- 一次真实只读抽查中跨店查询ReadTimeout，可选采集返回空参考和独立来源问题，未上传价格。完整实时Top3返回本轮尚未验收。
- 浏览器可视检查工具初始化失败，本轮未完成浏览器视觉验收；已完成组件SSR及生产构建。

测试先验证缺功能失败，再实现通过。独立审查三项均已补红→绿回归修正：按子SKU发现混挂链接；总耗时预算及现价余量保护；按店铺+父产品排除本链接、避免同一其他链接多规格凑Top3。重复在线现态仅参考观察不同也不构成核心价格冲突。

## 全量既有失败清单

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

上述核验和旧利润阈值断言、演示样本数量预期问题本轮没有改变业务逻辑去迁就。日志及基线逐项对照在 `runtime/sku-price-reference-integration-20261120/`，不入Git。

## 交付边界

仅本地WSL工作区与前端构建产物更新。未提交Git、未部署、未真实改价、未发送通知，现有自动提交开关未改。参考查询仍基于现有网页只读接口，集团权限及父子销量刷新一致性限制继续保留，不宣称已有完整专用开放接口。
