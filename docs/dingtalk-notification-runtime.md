# 钉钉机器人真实通知

2026-11-19 按用户要求接通运行。当前只接入已确认归属，不能将人员目录当作全量商品归属。

## 当前通道

使用独立应用“亏损提价助手”，调用官方 `POST /v1.0/robot/oToMessages/batchSend`，每条通知只给对应销售员 `userIds` 一人发送。`processQueryKey` 存为 Notice.task_id；回执通过 `GET /v1.0/robot/oToMessages/readStatus` 查询。同一收件人的 READ 才记已读；SUCCESS+UNREAD 才记送达。已受理及结果未知不重发。查询24小时内分级降频，不用工作通知接口，也不使用个人账号。

应用凭据由该应用独立读取后保存在本机忽略的 `.env`（权限600），不随 Git 提交。真实通知只需要 clientId 和 clientSecret；corpId/agentId 仍属于原登录配置，不能把通知已接通表述为端内 SSO 已验收。

官网合同：
- https://open.dingtalk.com/document/development/chatbots-send-one-on-one-chat-messages-in-batches
- https://open.dingtalk.com/document/development/chatbot-batch-query-the-read-status-of-messages

## 常驻进程

WSL `/home/developer/projects/loss-repricing` 内运行 `.venv/bin/python -m app.notification_worker`，PYTHONPATH 指向 backend。该进程只打开配置指定的真实档案、为 live 批次生成去重通知和处理 outbox，不加载价格上传适配器、不领取价格任务。现有只读网页继续独立运行。

健康状态：`runtime/notification-worker-health.json`。正常每5秒刷新（外部请求超时会延后），含模式、开关和各投递状态数量。异常正文不写健康文件，只记异常类型。通知按当前账号与目标归属重新核对；没有绑定或有歧义时保留待处理及原因。

Windows 任务计划注册被系统拒绝访问，实际使用当前用户 HKCU Run 自启动：`LossRepricingDingTalkNotifications`。部署脚本位于 `%LOCALAPPDATA%/LossRepricing/Notifications/supervise-notifications.ps1`，源码为 `deploy/supervise-notifications.ps1`。单实例隐藏守护，通知进程退出后等待15秒自动拉起。修改源码后需同步部署脚本并重启守护。

这台电脑需要开机、当前 Windows 用户已登录、网络和 WSL 可用。关机、注销、休眠期间不发送，恢复后从持久队列继续；这不是服务器24小时运行。正式模板标题为“亏损提价处理结果”，时间采用当前收件人记录中最近的处理时间（北京时间）；按“待处理、活动中、已提价、本轮不调”展示数量。活动中由 decision.reason_code=activity_locked 确认，其余待处理/待核验归入待处理，已核验对应已提价。待处理明细逐条列出 SKU、站点、店铺 ID 和原因，无明细显示“无”。只包含发送前已核对当前权限的商品，来源文本转义后展示。底部查询下载链接由用户指定为 http://8.133.252.166:50003 。每次发送的完整标题及 Markdown 存入 send_intent 证据。

## 人员与验收范围

用户已确认吴娟负责的商品 1732512893083550998，目标级归属已绑定吴娟的独立钉钉 userId，不赋予自动调价或修正权限。人员目录 `runtime/dingtalk_users.json` 中其余人员仅为候选绑定依据，尚不等于所有店铺/商品已确认归属。未来已绑定归属的 live 批次自动通知；其他负责人未绑定时须补充准确归属。

给常福生的联通测试已查到真实 READ 回执，证据存于 `runtime/dingtalk-connection-check.json`。业务推送回执存于真实档案 notices.delivery_evidence；不要把送达或已读当成业务处理完成或价格生效。
