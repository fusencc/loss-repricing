# 2026-11-23 费用采集与定价阻断范围验证

- 费用采集先验证原始分页，再排除不适用的授权类型并保留跳过诊断；同日只补查失败项，已成功范围沿用原检查时间。不回退昨日或 Excel。
- 公共费用排除类目配置，只有类目配置、缺少通用配置或通用配置歧义时阻断相应目标；泰国直发 VAT 仍取商品尾类目。
- 完整上一日快照在北京时间午夜后可短时评估，快照和在线价格观测至多 15 分钟。日期不一致、未来时间和混用旧汇率仍被拒绝；跨日未提交项仍重取重算。
- 字段异常只阻断对应子 SKU 或共享同一订单明细的目标；有效记录确认的主 SKU 归属矛盾仍阻断相应范围。海外仓费率及费用合计仅允许展示尾差，算价使用分项精确求和；仓库、采购、头程和库存矛盾仍逐商品阻断。

## 本地结果（WSL2 / Python 3.12.3）

- `cd frontend && npm run build`：通过。
- `.venv/bin/python -m pytest -q tests/test_daily_sales_fees.py tests/test_current_pricing_fees.py tests/test_sales_fee_model.py tests/test_sales_fee_inputs.py tests/test_live_online_preparation.py tests/test_pricing.py tests/test_formula_contract.py tests/test_erp_workflow.py`：187 项通过。
- `.venv/bin/python -m pytest -q tests/test_missing_overseas_stock.py`：10 项通过。
- `test_live_adapters.py`（子 SKU 字段异常隔离和主 SKU 归属冲突）、`test_review_regressions.py`（跨午夜提交）、`test_coordination_execution.py`（人工修正字段异常范围）按本次变更筛选：分别 6、2、4 项通过。
- 首次全量后端测试：971 项通过、10 项失败。失败涉及快照说明、库存样例、回读证据样例和仍按旧 0% 利润门槛断言的测试。已同步说明与测试，不改变 1% 门槛或真实核验条件；用 `pytest -q --lf --lfnf=none` 复跑全部 10 项，均通过。`git diff --check` 通过。

- 最终全量后端测试 `.venv/bin/python -m pytest -q`：983 项通过，1 条 Starlette 测试客户端依赖弃用警告（AnyIO `BlockingPortal` 别名），耗时 10 分 45 秒。

仅为本地构建及合成数据回归结果，未验证公司接口响应、真实调价、实际生效或部署。
