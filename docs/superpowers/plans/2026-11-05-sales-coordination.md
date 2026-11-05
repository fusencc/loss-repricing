# 销售协作 Implementation Plan

Goal: 在现有程序加入可追责、可接管、按范围授权的销售协作和定向通知。
Spec: ../specs/2026-11-05-sales-coordination.md
Architecture: 权限和管理状态存数据库；纯价格引擎不改；worker统一执行；钉钉工作通知outbox与网页共用记录。

## Global Constraints
保持Decimal/精确金额和各SKU独立规则；不发真实通知、不执行真实价格；人工接管不清未知锁；后端范围校验；外部改价来源待核实；保留所有当前未提交改动。

## Tasks
- [x] 1. 身份、归属与接管/修正API：models/coordination.py、services/coordination.py、api/auth.py、api/coordination.py、原routes.py、config/main及迁移d52e7b1c94a0。测试sales不能访问他人条目/批次/jobs/notifications，manager只指定范围，停用及改密码立即失效，归属/接管乐观版本并发。身份与API可单独拒绝越权。
- [x] 2. 通知适配与持久outbox：models/notifications.py、services/notifications.py、adapters/dingtalk.py、api/notifications.py、迁移e63f8c2d05b1（依赖d52）。测试投递前持久化、未知不重发、受理只查询、单人失败、主管升级、确认权限。服务enqueue_batch_notices(factory,batch_id)、dispatch_notices(factory,config)。
- [x] 3. 执行集成与冲突：services/execution.py、worker.py、services/corrections.py、tests/test_coordination_execution.py。测试改价10->12时旧10->11作废，接管后不提交、未知继续核验，live未证明独占/条件写入拒绝提交，修正权限被撤销不能执行，重复批准/任务不重复。
- [x] 4. Vue销售工作台：frontend全部；管理员用户/归属、销售限定范围、接管与修正、通知收件箱/确认、权限隐藏与错误提示、钉钉登录入口；构建、真实浏览器sales/manager/admin边界验证。
- [ ] 5. 集成审阅、迁移备份验证、顺序build/pytest/browser，补齐运维文档与待核实项；只合回本次增量。

## Shared interfaces
services.coordination: Principal(id,username,display_name,role,can_correct); resolve_ownership(session,key); can_access(session,actor,key)->bool; get_control(session,key,create=False); automation_gate(session,key)->(bool,reason); ensure_demo_ownership(factory,keys).
api.auth: require_user(Request)->Principal; require_admin(Request)->str; make_session(username,secret)兼容旧测试并支持版本化user会话。
后台不信任queued任务里的权限快照，执行前重新读取数据库。
通知模型与路由由任务2维护；root负责注册其模型、路由、worker集成，避免同文件并发。
修正API创建Correction，approve以dedupe_key=correction:<id>排kind=correction任务；root消费并执行。

## Review Focus
1. 同一操作者从过期链接操作已转交的目标必须拒绝。
2. 接管后已有在途请求无法取消，页面须告知仍核验，不能显示已停止外部请求。
3. 未配置实际DingTalk凭据不能显示真实发送成功。
4. 归属未知挂起本条，已核验历史保留。
5. 外部系统无法原子比较版本时，live自动不得宣称已解决所有并发覆盖。
