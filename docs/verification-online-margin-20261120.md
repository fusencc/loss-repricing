# 完整在线利润率阀门改为1%（2026-11-20）

用户确认先把现行统一阀门从0%改为1%。直发和海外仓共用，不单独引入热销8%，不把售价统一上涨1%。当前亏损候选口径见[核心计算规范](calculation-and-sources.md#5-erp-亏损筛选与新售价完整在线利润复核)。

## 当前实现

- `backend/app/domain/types.py` 的 `ONLINE_PROFIT_MINIMUM_MARGIN = Decimal("0.01")` 是统一策略值。
- 最低通过实际价 `B=b/(a-m)`，m=0.01，模型要求a>m。自动补亏仍为 `T=max(P+Δ,B)`，折扣反推与进档规则不变。
- 目标价、进档后价格、人工指定价及实际价格回读统一精确比较 `G(P)>=P*m`，价格误差容限不替代利润率阀门。
- 回读不足1%保留待核验和执行锁，暂停该站点新提交；结果未知不重传。
- 自动规则版本 `loss-online-margin-1pct-20261120-v7`；人工规则版本 `manual-online-margin-1pct-v3`；判定与回读证据保存最低利润率。
- 历史已存档记录不回写；待核验请求继续保留原请求证据，后续回读按当前统一1%门槛判断。

## 验证

先运行新增测试，旧代码实际给出25.00、允许12.50保本价，未满足1%边界；改动后通过。
独立回读测试证明原先12.65虽在目标价格容限内会被标记通过；修正后因低于1%保持mismatch并暂停站点。

WSL Python相关测试共250项通过：

- `test_one_percent_margin.py`、`test_online_profit_check.py`、`test_pricing.py`、`test_formula_contract.py`、`test_manual_live.py`：138 passed。
- `test_user_pricing_policy.py`、`test_sales_fee_model.py`、`test_current_pricing_fees.py`、`test_live_online_preparation.py`：39 passed。
- `test_execution.py`、`test_execution_safety.py`、`test_coordination_execution.py`：73 passed。

此前0%样本预期已按新口径更新，精确进档/循环小数/阀门两侧边界仍保留。海外接入样本由75.62变为76.81，手算最低价为((1.0557+.0532+1.4503)/.1042)/(.649589-.01)，五折反推向上到分。

本次未修改前端，因此未运行前端构建；未运行全量测试。仅本地工作区修改，未部署、重启服务、真实提交价格或发送通知。工作区既有其他修改保留。
