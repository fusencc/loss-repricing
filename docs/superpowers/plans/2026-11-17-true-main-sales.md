# True Main Sales Implementation Plan

> Execute inline with superpowers:executing-plans.

**Goal:** 按ERP真实主SKU归集销售子SKU七日销量。
**Architecture:** 替换sales_sales_window父商品求和，复用ERP目录采集，子SKU反查其他链接，保存逐条贡献。下游只认新basis。
**Tech Stack:** Python/httpx/pytest。
**Spec:** ../specs/2026-11-17-true-main-sales-design.md

- [x] 用混挂24=19+5、跨链接补1、目标20构造失败测试；覆盖未知、重复、分页限流和异常隔离。
- [x] 实现真实归属、子SKU扩展查询、同次去重和新来源合同。
- [x] 相关定价、采集、准备、执行/工作流回归，真实原件核对及小范围只读验证。
- [x] 同步规范和迁移说明，检查差异与复查。

验证记录：核心混挂测试初始11失败，替换实现后13通过；反查遗漏已知链接用例RED→GREEN。独立复查发现未关联规格可造成2件误作完整（实际32），新增失败用例后修复为参考值与完整性分离，相关36项通过；下游增加不放行回归。全量后端791项通过（464.95秒），前端生产构建通过；仅既有Starlette/AnyIO弃用警告。

Final: fixed 局部发现误作完整销量 — test_disconnected_true_main_sibling_cannot_make_partial_sales_complete RED→GREEN；下游参考值不放行回归通过；suite 791/791。最终只读样本XXHG667=243、GQHG18223=3，范围查询完成、全目录未证明，complete=false。未部署、未提交价格，保留当前工作区及其他既有变更。
