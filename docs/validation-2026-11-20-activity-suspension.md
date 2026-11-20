# 2026-11-20 暂停活动限制验证

## 变更范围

经用户明确授权，常规定价及人工修正不再以 `activity_locked` 阻断；删除通知中的“活动中”分类，网页“待处理”保持显示为“待处理”。营销价、折扣、固定营销价合同、不可售拦截、利润下限及实际有效价核验保持有效。活动来源取数和字段仍用于价格依据，不伪造无活动；历史记录不回写。

规则版本更新为 `loss-online-margin-1pct-activity-suspended-20261120-v8`，不保留旧活动拦截开关。

## 本地验证

环境：WSL2、Python 3.12。

- `.venv/bin/python -m pytest tests/test_pricing.py tests/test_notifications.py tests/test_formula_contract.py tests/test_one_percent_margin.py tests/test_coordination_execution.py tests/test_live_online_preparation.py tests/test_sales_platform_capture.py -q`：193 项通过；1 项 Starlette/AnyIO 弃用警告。
- 覆盖活动标记为真时常规算价继续、人工修正仍满足 1% 利润门槛、历史活动判定不被通知隐藏，以及来源、公式和执行回归。
- `frontend` 中执行 `npm run build`：类型检查和构建通过。

## 未验证与运行边界

未运行全量测试、浏览器人工验收或真实改价验证；未重启运行进程，未修改试点或真实提交开关。构建和测试成功不代表真实价格生效。已有活动拦截记录需重新评估，不能据本次规则变更自动标记成功。
