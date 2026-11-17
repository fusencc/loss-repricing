# Local history, sales window and fixed fees implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 按用户批注接入本程序历史、网页七日销量与固定Excel费率，并核实库存接口。

**Architecture:** adapters读取来源并保留证据；services只编排有效来源；domain继续统一补亏和折扣算价。旧来源路径删除，不保留双轨回退。

**Tech Stack:** Python 3.12 / Decimal / httpx / SQLAlchemy / pytest。

**Spec:** ../specs/2026-11-13-local-history-sales-window-fixed-fees-design.md

## Global Constraints
- 本程序已确认生效才开始72小时保护；未知执行锁定。
- 七日按网页指标，不编造日期；主SKU全链接求和。
- 费率用指定工作簿固定值；缺失来源不填0。
- 不开启真实提交，不修改无关前端工作。

## Review Focus
- 同主SKU多个产品链接、重复页和缺页不能少算销量。
- 本地档案为空与档案读取失败必须区分；外部公司观察不能影响保护。
- VAT、公开计划佣金、CNY成本/汇率与固定表源单元格必须一致。
- 直发TH缺类目VAT、SG缺站点费率、多个有库存仓只影响相应目标。
- 网页限流或错误响应不能标成0销量或0库存。

### Task 1: 本地历史与网页七日来源
- [x] 写失败测试：外部观察不再参与保护；多链接0+21按21保护；缺页保持未知。
- [x] 实现 `capture_sales_window(settings,capture,targets,...)` 于sales_sales_window.py，替换ERP窗口采集；在线模型改为sales_observed_at及sales_basis。
- [x] 精简local_price_history、live_online_preparation、erp_workflow与preview引用，删除未使用外部历史路径。
- [x] 运行相关测试并记录结果。

### Task 2: 固定表与利润公式
- [x] 写失败测试：固定表VAT/公开计划佣金、直发重量分档、缺配置、错误缓存不参与计算。
- [x] 导入指定Excel至独立runtime固定配置；实现固定读取与公式，停止动态费率抓取。
- [x] 记录库存接口只读核实结果；说明含VAT上传价位置与d的意义。
- [x] 运行费用、在线准备、公式与工作流测试。

### Task 3: 交付核验
- [x] 更新现行规范、配置合同及示例，清理废弃路径引用。
- [x] 先前端构建再后端全量测试；核对真实只读抽样与离线验证边界。
- [x] 一次独立代码复查，修复影响本次需求的问题。
- [ ] 报告逐条批注结果、实际接口状态和仍缺少的输入。

## Execution ledger
- Ruling: 在现有feat/initial-app工作区增量完成，因为最新版业务实现尚未提交；不自动打包或提交其他任务变更。
- Ruling: 用户本轮明确要求开始修改，按已给出的五条批注直接执行；不重复请求方案许可。

- Task 1/2 evidence: initial RED 8 cases (missing web module + legacy external-history/VAT behavior); targeted 78 passed in35.69s after implementation. Real fixed workbook imported, four overseas site totals independently checked using synthetic product costs.
- Read-only evidence: new web collector returned two links totaling242 for XXHG667; official inventory HTTP200/code200/success=true with7 warehouses; current sales variants recovered HTTP200/resp_code0 with6 children and A1stock563. Artifacts under runtime/user-policy-20261113.
- Build: frontend production build passed. First full run:736 passed/13 failed, all failures traced to one obsolete HTTP mock asserting removed date-window query parameters; corrected and HTTP61 tests passed.
- Final review: one independent read-only reviewer found2 P2 issues. Both reproduced RED: known-main bad quantity blocked unrelated mains; generic HTTP still required external history fields. Fixed per-main isolation and removed live external history mappings; no secondary reviewer or extra business gates.
- Ruling: generic HTTP snapshots start history-unconfirmed and PricingService reads the authoritative database; file snapshots retain explicit reconciliation inputs because this is an existing separate mode, not a fallback.
- Ruling: bump rule evidence version to loss-local-history-web-sales-20261113-v3 for source/guard policy change; fixed-fee content version remains independently hashed.

- Final verification: review-fix suite152 passed/1 dependency warning; final complete backend suite751 passed/1 dependency warning in452.55s. Frontend build, compileall and git diff --check passed. No live price submissions or deployment.
- Task 1: complete — own live history and web sales source; Task 2: complete — fixed table import, both fee formulas and read-only interface evidence; Task 3: verification/review complete, delivery in current response.
- Optional scope clarification: all7 workbook public-plan commissions are10%; asked whether to use globally. No answer at delivery, so retain exact product binding; do not infer global commission authorization.
- Integration ruling: leave changes in the existing feature workspace for review; no automatic commit/merge/push of this workspace's unrelated uncommitted work.
