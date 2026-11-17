# 提价记录首页实施计划

> **For agentic workers:** Use superpowers:executing-plans for inline implementation.

**Goal:** 新增默认当日的提价记录首页。
**Architecture:** 公开日期查询 API 提供展示字段、汇总和分页；RecordsPage 免登录展示及只读详情；原管理接口独立认证。
**Tech Stack:** Vue 3 / TypeScript / FastAPI / SQLAlchemy。
**Spec:** docs/superpowers/specs/2026-11-13-records-home-design.md

## Global Constraints
- 不改价格公式、执行逻辑或已有未提交工作。
- 不将演示记录、已计算或受理误标为真实生效。

## Review Focus
- 日期边界与历史批次不受最近记录数量上限截断。
- 匿名可读销售记录；管理接口仍拒绝匿名写入，公开字段不包含内部证据。
- 切换日期/分页时旧请求不能覆盖新请求。
- 空数据及请求失败清晰可见，不能展示伪造数据。
- 窄屏及减少动态效果偏好可用。

### Task 1: 日期记录 API
- [x] 编写日期范围、分页、汇总及认证测试并观察失败。
- [x] 实现免登录 GET /api/records，按批次业务日期筛选，投影显示字段并统计分页。
- [x] 运行新增 API 测试及相关认证回归。

### Task 2: 单页展示
- [x] 新增 RecordsPage.vue 和 records.css，加入今日/周/月、搜索、日期、状态、分页、详情及主题。
- [x] App 默认 records 且无需登录；公开记录详情为只读，移除首页账号与管理入口。
- [x] npm run build；检查真实浏览器交互、窄屏、空数据和错误状态。

## 验证结果
- 后端全量：762 passed，1 条既有依赖弃用警告。
- 日期边界：3 passed；前端 TypeScript 与 Vite 构建通过。
- Chrome：默认当日/深色、周/月、浅色、日期筛选、搜索、状态、分页、错误态和管理入口通过。
- 手机空态及有表格场景已检查；修复隐藏表头定位造成的横向溢出。
- 样例交互只通过浏览器路由隔离模拟，未向数据库添加样例记录；真实业务执行未验证。
