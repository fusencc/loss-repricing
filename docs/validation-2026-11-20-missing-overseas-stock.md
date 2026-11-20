# 2026-11-20 海外库存缺失使用直发模型验证

## 当前变更

- 用户授权库存缺失不再待处理：缺失字段、null及空字符串选择直发模型，规范库存仍为null，不填0。
- 常规计算及人工修正保存计算告警，商品详情显示告警。库存为正用海外仓，等于0用直发；负库存、无效值、归属不明确及其他独立证据不足仍阻断。
- 更新规范模型、来源准备、直发费用入口、仓库精确匹配、公式版本及规范快照JSON Schema。历史记录不回填，不改变真实提交授权。

## 本地验证

WSL2 / Python 3.12：

- `frontend` 下 `npm run build`：通过，包括 TypeScript 检查。
- 项目根目录 `.venv/bin/python -m pytest tests/test_missing_overseas_stock.py tests/test_online_profit_check.py tests/test_live_online_preparation.py tests/test_fixed_pricing_fees.py tests/test_erp_order_loss.py -q`：89项通过。
- 首轮相关测试中旧用例仍要求未知库存抛错，已按此次明确授权移除该旧预期，并新增缺失字段/null/空字符串、告警、正常库存不告警、负库存及仓配冲突、人工修正回归。
- 额外全量测试曾出现失败标记；执行较久后主动中断，未取得完整失败归因，因此不宣称全量通过。

## 未验证边界

未做浏览器实际交互验收、真实来源重采集、生产进程更新、真实价格提交或有效价核验。合成测试通过不代表真实环境已生效。
