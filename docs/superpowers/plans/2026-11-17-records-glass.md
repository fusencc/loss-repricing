# 部门提价日志玻璃页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 01 极光通栏，部门切换后展示销售员，中央弹窗显示本人全部详细日志。
**Architecture:** 后端只投影保存的 Item/Batch/Execution 与当前归属；Vue 按人员 ID 分组和打开 ElDialog，不重算价格。
**Tech Stack:** FastAPI / SQLAlchemy / Vue 3 / TypeScript / Element Plus / CSS。
**Spec:** `docs/superpowers/specs/2026-11-13-records-home-design.md`

## Global Constraints

WSL 主仓库；保持现有未提交工作。无分页；不改变提交、权限和公式；归属缺失不猜测；金额保持十进制文本。

## Review Focus

部门来源缺失/冲突；同名人员隔离与目标归属；快速切换/失败不串数据；模拟/未生效标识；弹窗键盘和窄屏长内容。

### Task 1: 只读记录合同

Files: `backend/app/api/routes.py`, `backend/app/services/records.py`, `tests/test_records_api.py`。
Interface: `GET /api/records?department=<id>` 返回 departments、department、total、counts、shops、items。item 包含 seller、department、decision、execution 展示字段。

- [x] 写并运行无日期查询、部门隔离、超过 100 条、目标归属、部门证据、白名单测试，确认旧实现失败。
- [x] 实现新投影和路由，运行 `.venv/bin/python -m pytest tests/test_records_api.py -q`。

### Task 2: 页面和中央弹窗

Files: `frontend/src/RecordsPage.vue`, `frontend/src/records.css`, `frontend/src/records-model.ts`, `frontend/tests/records-model.test.mjs`, `frontend/src/App.vue`。
Interface: `groupBySeller(items)` 按稳定 ID 返回每组全部记录和四态数量。

- [x] 先验证同名账号隔离、记录不丢失、金额原样传递。
- [x] 替换页面/样式，删除 records-period 工具和失效测试。运行 `node --test tests/*.test.mjs`、`npm run build`。

### Task 3: 联调交付

- [x] 临时 demo 数据库浏览器验收部门切换、开关弹窗、错误、空数据及 320/390/768/1440px，保存截图。
- [x] 后端测试和独立审阅，记录验证边界。

## 执行记录

- 用户已选择 01 并要求开始改造，在当前 `feat/initial-app` 实施。现有记录页及关联后端尚未提交，保持其他开发工作，不整体提交或复制旧实现。
- 按每位销售员一行、弹窗查看本人全部日志实施；此前已发可选澄清。
- 本地后端全量回归 794 项通过；独立审阅后的日志接口专项 8 项通过，人员分组与时间文案 3 项通过，前端生产构建通过。依赖仅有现存 Starlette/AnyIO 弃用警告。
- 独立审阅发现并修复两处展示语义：保留时间依据，区分来源生效时间与首次核验保护起点；按更新时间排序，避免旧批次的延迟核验被旧业务日期压到后面。新增回归先失败、修复后通过。
- `deploy/serve-records-demo.py` 使用临时数据库、隔离运行目录、模拟数据且关闭真实提交；`PYTHONPATH=. .venv/bin/python deploy/serve-records-demo.py --port 18827` 可启动此验收页面。页面与接口均为当前实现。
- `deploy/smoke-records.cjs` 通过：点击整行/姓名、键盘打开、中央弹窗、关闭及 Escape 焦点返回、全部日志、快速切换防乱序、错误恢复、空数据、四种视口无横向溢出和无脚本异常。默认使用本地 Playwright，可通过 `PLAYWRIGHT_MODULE` / `CHROME_EXECUTABLE` / `RECORDS_SCREENSHOTS` 指定环境。
- 验证边界：本地代码与隔离 demo 完成验收，未部署、未接入真实人员/部门样本验收，也未触发真实提价或通知。部门/人员证据缺失会显示待确认部门/待分配。

## 2026-11-17 精简优化

- 按用户补充要求固定四部门行内按钮、三种状态文案与紧凑日志；删除弹窗日期/核验元信息/四价存档/批次任务字段及不再使用的样式。
- 空数据库仍返回四部门，允许空部门直接切换；未推断缺失归属。
- 本轮专项后端 8 项、前端 4 项通过，生产构建通过，浏览器检查覆盖 1440/768/390/320px、直接切换、空部门、乱序响应、错误恢复、弹窗删字段和状态名称。未重复运行全量回归。

## 真实记录接入（2026-11-17）

- 用户要求显示今天吴娟商品1732512893083550998的真实提价记录。已直接读取权威 live_history 数据库，展示4条真实过程记录，其中1条已核验成功；不将过程记录数量当成成功提价次数。
- 新增 deploy/serve-records-live.py：SQLite mode=ro、仅本机监听、仅提供 health/records/静态前端，不挂载管理或价格执行路由，不启动 worker。相同18827端口已停止模拟服务并切换此入口。
- runtime/records-display-owners.json 保存用户明确确认的完整商品键→吴娟展示绑定，不写账号、Ownership或权限。此文件不入Git；缺失则显示待分配。
- 来源实际部门TIKTOK十二部规范显示为Tiktok十二部；真实人工记录币种取完整商品键匹配的online_rows.currency一致值，不按站点猜测。
- 验收真实页面：吴娟、4条live记录、已提价1条、上传87.18 PHP、回读34.88 PHP，无模拟记录；写入路径404。前端构建通过，专项接口含部门大小写/币种/live过滤回归。
- 启动：在项目根目录运行 `.venv/bin/python deploy/serve-records-live.py --port 18827`。访问 `/?department=department%3ATiktok十二部` 可直接查看对应部门。

## 成功结果与完整过程（2026-11-17）

- 用户纠正：吴娟同一商品准备和失败尝试不独立计数，也不能当作活动中；只展示最终成功的一条，点击查看完整过程。
- 只读投影按同业务日、完整目标键和相同价格方案归并唯一成功之前的准备/明确失败记录。多次成功、不同店铺、不同价格、成功后新记录、未知执行保留独立。原数据库证据不修改。
- 人员弹窗提供原生details/summary完整过程，默认折叠，按时间显示准备/失败/成功及原因。
- 真实页面验证：吴娟1条已提价、process含4步骤，0条活动中；展开可见失败说明。后端10项、前端4项、构建通过。
