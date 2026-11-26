# 2026-11-26 固定七人任务删除与通知姓名查询

## 变更

- 删除 ERP 七人姓名／部门常量、名单日期设置、专用调度与 `--roster-date` 参数。
- 删除名单按人拆分、正式提交、零库存续跑及保护日志新增模块；批量采集删除 `roster_salespeople` 分支，保留通用按销售员及按部门采集。
- 执行服务删除新名单提交分支；历史 `erp_roster_upload` 执行只保留任务查询，未知结果不重传，商品锁和保护不变。
- 原名单续跑及名单通知遗留任务由 worker 标记取消，避免落入普通价格提交。
- 业务通知的实际 `dispatch_notices` 路径接入本项目 `LR_RUNTIME_DIR/dingtalk_users.json`：按保存的 ERP 销售员姓名查询；没有该字段时按当前已授权启用账号姓名查询。保留网页归属、单条人员冲突隔离、独立管理员告警和未知发送不重发。
- 删除固定名单钉钉 ID 映射及专用开关。目录内同名歧义／无匹配时仅该通知保留原因，不猜测收件人。发送后查询仍使用原实际收件人 ID。
- 历史批次查询、下载与已提交记录核验保留，不清理实际业务数据。

## 本地验证

均使用 WSL `.venv/bin/python -m pytest`，外部接口使用测试替身；本次未执行真实价格上传、钉钉推送、人员目录刷新或服务器部署。

- 工作流、部门采集、费用、历史记录 API／流及执行回归：112 passed，3 条依赖弃用警告。
  `tests/test_worker.py tests/test_erp_workflow.py tests/test_erp_department_workflow.py tests/test_daily_sales_fees.py tests/test_records_api.py tests/test_records_stream.py tests/test_review_regressions.py`
- 执行、执行安全、协作及历史名单回查：110 passed。
  `tests/test_execution.py tests/test_execution_safety.py tests/test_coordination_execution.py tests/test_roster_price_upload.py`
- 最终实际通知分发、调度、目录解析与通知回归：73 passed，1 条依赖弃用警告。
  `tests/test_notification_directory_dispatch.py tests/test_worker.py tests/test_dingtalk_directory.py tests/test_notifications.py`
- 批量采集：60 passed；销售员归属及分页恢复两套更新测试：32 passed。
- 批量采集与扩展适配器测试合并：134 passed，21 failed，1 条依赖弃用警告。
- 最终全项目仅收集检查：1233 tests collected，无导入／收集错误；未执行全项目全部测试。

上述测试组有交集，不将通过数相加作为唯一用例总数。

## 扩展测试剩余失败

- 20 项桌面相关失败：独立脚本生成器把使用默认参数 `wait=sleep` 的函数放在 `sleep` 导入前，生成脚本加载报 `NameError`。本次未修改桌面构建器。
- 1 项导出映射断言失败：`test_raw_and_approved_evidence_cannot_silently_disagree[sku-evidence_target_mismatch]` 中已有 SKU 证据冲突，但相关决策仍为 `ready`。本次未修改该映射路径，留待单独处理。

以上失败不以恢复固定名单、整页拒绝或整批阻断来修补。
