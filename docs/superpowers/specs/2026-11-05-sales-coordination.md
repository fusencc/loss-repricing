# 销售协作与通知设计

用户已选择“带权限的网页＋钉钉通知”并要求补足功能。此文把已讨论方案落成实施合同，不重复请求同一授权。

## 范围与依据
新增销售员/主管权限、商品归属、人工接管、修正方案、价格冲突拦截、通知收件箱与钉钉工作通知。保留现有算价公式、未知提交防重、四种结果及逐条核验。
用户回答销售员可能在公司销售系统和平台后台都会改价，随后要求“先标记，询问后续核实”：这条信息属于待核实，不能据此伪造两个系统的历史接口或操作者证据。
真实价格接口继续关闭。钉钉使用企业内部应用定向工作通知；本轮只模拟投递，提供可配置、可测试的真实适配实现，不向真实人员试发。不创建钉钉应用或复制其他项目凭据。

## 方案
已比较机器人为主、多维表格＋通知、权限网页＋通知，用户选择最后一种。网页为权威处理入口；通知只提供摘要和链接，表格不成为第二套可直接执行的价格数据。

## 业务规则
1. 用户分admin/manager/sales；管理员保留原独立账号。销售员只见自己归属，主管只见明确指定给自己的归属；后端每次校验，不依赖前端隐藏。
2. 归属以平台/店铺/站点为默认范围，允许具体实际在线子SKU覆盖；一条范围只有一个负责人，主管可单独指定。负责人须有效。自动托管需管理员明确开启，归属缺失不提交。
3. 管理状态auto/manual/review与执行结果独立。人工接管即阻止尚未发送的自动意图；已提交/未知继续核验，不清锁、不回滚。交回自动是明确动作，不因72小时到期恢复。
4. 销售员可接管、暂停、提交修正、查看通知并确认。can_correct决定能否批准自己的合格修正；主管可批准范围内修正。低于零正常毛利的方案不执行，活动/仓配/未知结果等核心检查仍适用。人工明确修正可绕过自动72小时冷却，但不绕过未知锁、保护、身份、费用或活动检查。
5. 修正先保存目标实际售价、原因和当时在线价格依据，经批准入持久任务；执行时重新取当前数据、按最新费用复核，价格/折扣/仓配或版本改变即使仍亏损也使原方案失效。重复批准不能重复排队，重复任务不能重复上传。
6. 自动方案计算后，如果提交刷新发现现价/折扣/仓配/已知变更版本不同，停止该SKU旧方案，置review；不能直接在新人工价格上再次叠加补亏。没有操作者证据时只称“外部变更”。
7. 服务保存每次目标的价基线；发现与最近已核验实际价格不一致时暂停该目标，不能把平台自然折扣变化归咎于销售员。人工明确交回后重取、建立新基线再算。
8. 外部接口未确认支持条件更新，live自动与修正需要额外确认独占写入范围或原子价格版本合同；本轮不声称只靠读前检查就绝对防覆盖。两个外部系统的接入保持待核实。
9. 通知按责任人汇总一个批次；异常优先。无法确定负责人时管理员网页待分配，不猜收件人。发送受理、已送达/已读（来源有证据时）、业务确认、问题解决、价格生效分别留证。
10. 通知outbox持久唯一键；发送前记sending。超时/崩溃/响应未知不自动重发；有task_id只查询。明确失败可人工重试，已受理/未知不可重发。未确认异常24小时后仅向指定主管创建一次升级通知。每条链接再次鉴权。

## 数据与接口
新增UserAccount、Ownership、TargetControl、Correction及Notice。管理员账户以特殊principal id=admin表示，允许作为演示归属负责人，不能把此特殊身份通过普通用户创建接口注册。
Ownership: id, scope_key(unique), platform, shop_id, site, target_id(optional), owner_id, supervisor_id(optional), autonomous(bool default false).
TargetControl: target_id(PK), key(JSON), mode(auto/manual/review), version(int), reason, changed_by, updated_at, baseline(JSON), handback_pending(bool).
Correction: id, target_id, item_id, desired_actual(string Decimal), reason, requested_by, state(proposed/queued/executing/verified/failed/stale), control_version, baseline(JSON), job_id, execution_id, created_at, updated_at, result.
Notice: persistent recipient/summary/item refs, delivery state, acknowledgment, escalation, task/request IDs; no credentials or access-bearing URLs in evidence.

API合同（全部/api）：
- GET auth/me => {id,username,display_name,role,can_correct}; POST auth/login保留并支持本地销售账号；POST auth/dingtalk {code}仅为预先绑定userid登录，未知人不能自注册。
- GET/POST users；PATCH users/{id}（管理员）；密码仅输入写入散列、不返回。禁用用户/换密码后旧会话失效。
- GET/POST ownerships；PATCH ownerships/{id}（管理员）；GET scopes供当前人查看归属。
- GET items/{id} 增加 control、ownership、capabilities；原overview/batches/items/jobs/protections均按登录人过滤，原settings/contracts/run/reconcile只管理员。
- POST items/{id}/control {action:takeover|pause|handback,reason,version}，409表示陈旧版本；handback清待协调、仍保持执行锁。
- GET/POST items/{id}/corrections，POST corrections/{id}/approve {version}；创建即原子人工接管；批准只排队，worker再校验当前权限和范围。
- GET notifications；POST notifications/{id}/ack；POST notifications/{id}/retry（仅明确失败）；GET notification-status（管理员）；POST notifications/dispatch（管理员，演示投递调试）。

## 实施与验收
使用现有FastAPI/SQLAlchemy/MySQL迁移/Vue。新模型独立模块，核心算法不引入UI权限。本地密码用salt+scrypt。钉钉应用秘密仅环境变量，免登码不入日志。
验证越权直接请求、停用/改归属、接管与排队竞态、陈旧修正、外部改价、未知执行不重传、通知超时与重复请求、主管升级、登录/网页作用域、现有回归。真实接口与公司负责人表未联调时交付清楚显示未配置。
