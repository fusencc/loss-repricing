# 亏损补亏工作台（loss-repricing）

针对亏损订单对应的在线子 SKU，自动完成数据采集、补亏算价、保护检查、价格上传与回读核验。系统已接入真实 ERP、销售后台、每日费用表、官方调价日志与钉钉通知，并已部署进入真实业务验证阶段。

主要开发副本位于 WSL2 的 `/home/developer/projects/loss-repricing`（Windows 旧目录 `D:\projects\loss-repricing` 不自动同步）。

## 1. 当前运行能力

- **数据采集与算价**：按部门或名单采集 ERP 候选订单（亏损/禁止发货 × 可配货/待处理）、订单明细、在线商品现态、同 SKU 营销参考价、当日汇率与每日费用表，单据级隔离异常（缺项只跳过单条，不阻断同批其他商品）。
- **核心计算口径**（完整公式与变量见 [docs/calculation-and-sources.md](docs/calculation-and-sources.md)）：
  - 统一使用 `Decimal` 计算，严禁 `float`。
  - 取同在线子 SKU 有效负亏损订单的最小单件亏损幅度，按当日汇率折算为站点币并叠加到当前实际营销价。
  - 按海外仓库存自动分流直发/海外仓费用模型；无同 SKU 营销参考价时按 1% 最低在线利润率校正，有参考价时以参考价为硬上限优先。
  - 按当前成交折扣反推含税上传价，并按站点步长取整。
- **执行与回读核验**：
  - 仅保留核心安全底线：防重复提交锁（`VariantGuard`，提交结果未知时只核验不自动重传）、近 72 小时官方调价日志保护、人工保护名单（`Protection`）、正数金额与币种/SKU 身份一致、跨北京时间业务日停止旧日提交。
  - 单条上传前做单品现价预检，不因单条异常或回读超差熔断整个站点或整批任务。
- **提价日志与通知**：前端按部门展示销售员提价记录摘要与明细弹窗；钉钉工作通知负责推送执行与异常提醒（详见 [docs/dingtalk-notification-runtime.md](docs/dingtalk-notification-runtime.md)）。
- **只读对账模式**：支持 `file` 模式读取规范 JSON 快照离线算价对账，不执行外部上传。

## 2. 目录结构

- `backend/app/domain/`：领域模型与纯算价规则
- `backend/app/adapters/`：ERP、销售后台、费用、日志、上传与通知适配器
- `backend/app/services/`：工作流编排、执行锁、核验、记录查询与通知服务
- `backend/app/worker.py`：后台定时任务、租约与核验轮询
- `frontend/`：Vue 3 + TypeScript + Element Plus 管理页面（仅展示，不重算价格）
- `tests/`：本地离线单元测试与回归测试
- `deploy/`：本地启动与部署脚本
- `runtime/`：本地数据库、运行产物与日志（不入 Git）

## 3. 本地启动与只读测试

在 WSL2 项目根目录执行：

```bash
# 启动本地工作台（API + worker，默认关闭真实提交）
bash deploy/start-wsl.sh