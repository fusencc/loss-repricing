# 销售协作与钉钉通知操作说明

更新日期 2026-11-09。

本次在原自动补亏程序中加入销售员/主管账号、商品归属、接管与交回、修正方案及通知收件箱。网页是处理入口，后端每次按当前账号和归属鉴权。钉钉采用**企业内部应用的定向工作通知**：按已绑定 userid 发送四类处理状态的数量和单条通知链接，使用 action_card 格式；没有另建企业机器人或第二套可执行价格台账。

当前交付包含模拟流程和可配置真实适配器。真实人员账号及归属、企业内部应用的权限和SSO发布配置、实际工作通知发送均未验收；不得把demo、接口测试或本地网页记录称为真实送达。真实调价接口和数据源接入仍受原有合同与开关约束。

## 配置与启用顺序

1. 管理员先创建有效的销售员和主管账号。销售员只看自己范围；主管只看明确指派给自己的范围。根据实际授权设置can_correct，不把所有销售员默认设为可自行批准。禁用账号或修改账号后，已有会话按版本失效。
2. 登记平台、店铺、站点归属，必要时配置实际在线子SKU目标的覆盖归属。每个范围只有一个负责人；主管需单独明确。没有有效负责人时，不自动提交；通知留在管理员网页待分配，不猜测钉钉收件人。演示用特殊管理员身份不能通过普通用户接口创建。
3. 确认人员和商品范围后，由管理员明确开启该归属的自动托管。还须满足全局试点、站点、暂停和原有资格检查；开启归属本身不会开启真实调价。
4. 如需钉钉端内登录，由企业管理员配置本项目专用企业内部应用，添加网页应用能力，设置首页和PC首页、端内免登地址及重定向URL，并发布给明确人员范围。运行JSAPI的域名须与应用配置一致；员工设备与服务器均须可访问正式网页。
5. 核对并绑定每位已注册用户的真实钉钉userid。钉钉免登只允许预先绑定的有效账号，未知人员不能借免登自动注册；不得用姓名猜userid。不读取或复制其他项目凭据。
6. 默认保持通知demo。完成应用、域名、可见范围、绑定与接口权限核查后，另行开展真实通知验收；在此之前不要将enabled/live配置当成验收完成。

环境变量见[.env.example](../.env.example)，含义如下：

| 变量 | 默认/要求 | 含义 |
|---|---|---|
| LR_COORDINATION_REQUIRED | true | 本地默认启用协作权限；生产强制true。false只用于隔离旧测试，不是业务上的免权限运行方式。 |
| LR_LIVE_WRITER_EXCLUSIVE | false | 真实写入独占条件尚待核实。只有外部来源、人工改价入口及独占写入约定得到证实后才可评估开启；它不能凭空赋予外部接口原子比较更新能力。 |
| LR_NOTIFICATION_MODE | demo | demo仅生成可见模拟记录；live表示调用真实适配的模式，仍需enabled及完整配置。与调价ADAPTER_MODE分开。 |
| LR_DING_ENABLED | false | 真实钉钉通知开关。关闭时live不发送。 |
| LR_DING_CORP_ID | 留空 | 企业CorpId。 |
| LR_DING_CLIENT_ID | 留空 | 本项目企业内部应用Client ID，对应服务端token接口appKey。 |
| LR_DING_CLIENT_SECRET | 留空 | 本项目应用Client Secret，对应appSecret；仅服务端环境配置，不提交Git、不出现在网页或日志中。 |
| LR_DING_AGENT_ID | 留空 | 发送工作通知的微应用AgentId。 |
| LR_PUBLIC_BASE_URL | 留空 | 员工可访问的HTTPS网页地址，不包含凭据或查询参数。 |
| LR_LIVE_SUBMIT_ENABLED | false | 原真实价格提交开关，与通知配置独立；本轮不以开启通知代替调价上线验收。 |

新增迁移顺序为d52e7b1c94a0（账号/归属/控制）后e63f8c2d05b1（通知outbox）。生产按已有部署流程备份数据库、停止worker、执行迁移后再启动；不能把本地SQLite建表当作生产迁移完成。

## 销售员日常处理

“接管”或“暂停”需要原因并核对当前版本，会阻断尚未发送的自动意图。已提交、已受理或结果未知的在途请求继续核验，保留原锁；页面接管成功不表示外部请求已取消。接管不会在72小时后自动结束。

“交回自动”是明确操作；系统重新读取当前数据、建立基线，再按现有规则评估。交回不会清除未知提交锁、跳过活动/仓配检查或直接恢复一条过期方案。遇到版本冲突须刷新页面，查看新状态后再决定。

修正填写的是目标实际售价及原因，创建方案同时转为人工接管。**保存方案、批准排队、执行上传、价格核验分别是不同阶段。** 指定主管可批准自己范围内方案；销售员只有在can_correct授权和当前权限都满足时才能批准自己的合格方案。批准不是立即改价：worker执行时重新核对人员、归属、最新费用及在线价基线。修正价的完整在线利润率未达到1%、价格/折扣/仓配/已知版本改变、未知锁或其他保护不满足时，不执行原方案。人工明确修正可跳过自动72小时冷却，但不能绕过这些核心检查。

自动方案计算后发现线上数据改变，或与最近核验基线不一致时，停止对应旧方案并转待协调。没有可靠操作者证据时只称“外部变更”，不能归因于销售员或把自然折扣变化称为人工操作。

用户所述“公司销售系统和平台后台两边都会改价”目前是**待核实信息**；询问清单见[销售改价待核实事项](pending-business-confirmations.md)。两套系统的实际入口、同步关系、历史接口、操作者证据和条件更新能力均未由此得到确认。读前检查与本程序数据库锁不能阻止所有外部并发写入；LR_LIVE_WRITER_EXCLUSIVE默认false，在真实独占/原子更新边界确认前不宣称已解决覆盖风险。

## 通知状态与异常处理

钉钉卡片的「查看详情」和收件箱的「查看涉及商品」进入同一个通知页面。打开深链后先登录，再按当前归属读取关联商品。待处理、待核验优先显示。详情同时显示计算目标实际价、回读实际价和处理原因，分别列出投递状态和业务确认。报告按当前商品数分页，关联条目减少时自动回到有效页。

按批次、责任人汇总通知；发送前按当前归属重新筛选条目并重算摘要。已无可访问条目时取消发送；没有明确有效主管时不猜测升级收件人。异常超过24小时未确认，仅向明确指定主管创建一次升级通知。网页链接和确认动作再次鉴权；旧链接不会保留原归属权限。

| 状态/证据 | 可以说明什么 |
|---|---|
| demo / synthetic=true | 本地演示模拟，未向真实钉钉人员发送。 |
| pending | 已持久登记，尚未提交，可能仍缺配置或userid绑定。 |
| sending | 提交意图已先持久化，发送进行中。 |
| accepted | 钉钉创建异步任务并返回task_id；不能据此称用户已收到或已读。 |
| delivered | 结果查询明确该userid位于未读名单，具有送达且未读证据。 |
| read | 结果查询明确该userid位于已读名单；不表示点击网页或完成业务处理。 |
| failed | 接口或逐人结果明确失败；管理员可人工发起重试。 |
| unknown | 发送超时、响应不完整、进程中断等导致结果未知；禁止自动或人工重发。 |
| cancelled | 发前已无当前有权查看条目，取消该通知，不向旧负责人发送。 |
| ack_at / ack_by | 网页上的业务确认，独立于钉钉已读，不代表问题解决或价格生效。 |

本地通知唯一键防止重复入队。发送前先保存sending；有task_id的受理记录只查原任务，查询失败不转为重发。仅明确failed可由管理员重试；已受理、已读、未知均不可重发。发送与查询的任务ID/请求ID保存在白名单证据中，不保存原始响应、授权码或带凭证URL。

手动投递入口与worker共用独立notification_dispatch数据库租约；并行调用返回busy，不会恢复另一活动发送。过期发送按未知处理。通知发前检查后或外部发送已在途时，仍可能发生归属转移；外部卡片携带通用标题、数据模式、状态计数和通知编号。商品、处理原因和价格保留在鉴权网页；已在途的摘要无法撤回。网页商品详情和动作始终按当前后端权限校验。

管理员可通过GET /api/notification-status查看模式、配置完整度及各状态数量，POST /api/notifications/dispatch触发一次当前模式投递。员工通过 GET /api/notifications 查看本人可见记录，通过 GET /api/notifications/{id} 分页查看详情，通过 POST /api/notifications/{id}/ack 确认；管理员通过POST /api/notifications/{id}/retry仅重试明确失败。业务确认、异常解决和价格生效应分别看对应处理及核验证据。

## 钉钉官方接口合同与验收证据

2026-11-05已只读核实官方文档。服务端固定使用下列HTTPS地址，不接受任意调用地址。采用企业内部应用工作通知即可实现定向摘要、网页链接和逐人发送结果，首期无需额外机器人。

| 用途 | 实现接口 | 核心字段 |
|---|---|---|
| 应用访问凭证 | POST https://api.dingtalk.com/v1.0/oauth2/accessToken | 请求appKey/appSecret；响应accessToken/expireIn，服务端内存缓存。 |
| 端内授权码 | 前端dd.requestAuthCode | corpId/clientId；成功得到code，5分钟有效且仅一次使用。 |
| 用户身份 | POST https://oapi.dingtalk.com/topapi/v2/user/getuserinfo | access_token调用凭证，body code；errcode=0后读取result.userid/name。 |
| 定向工作通知 | POST https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2 | agent_id、userid_list、to_all_user=false、msg action_card；响应task_id/request_id只是受理。 |
| 逐人发送结果 | POST https://oapi.dingtalk.com/topapi/message/corpconversation/getsendresult | agent_id/task_id；按目标userid检查失败、限制、已读、未读名单。 |

官方依据：[企业内部网页应用免登](https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free.md)、[应用accessToken](https://open.dingtalk.com/document/development/obtain-the-access-token-of-an-internal-app.md)、[通过免登码获取用户信息](https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free.md)、[发送工作通知](https://open.dingtalk.com/document/development/asynchronous-sending-of-enterprise-session-messages.md)、[消息格式](https://open.dingtalk.com/document/development/message-types-and-data-format.md)、[逐人发送结果](https://open.dingtalk.com/document/development/gets-the-result-of-sending-messages-asynchronously-to-the-enterprise.md)。

工作通知结果仅能查询24小时内任务，收件人超过100不支持结果查询；当前程序每条通知单一收件人。官方同人同内容每日一次、内部应用每人每日500条等限制可能使发送接口受理但实际不发送，须以逐人结果区分。过期缺证据保持原受理或未知，不推断失败并重发。进度接口getsendprogress未用于本实现：其官方页面正文7天与参数24小时说明不一致，不能据此承诺延长逐人结果保留期。

真实验收至少需要明确企业/应用、已发布可见范围、人员绑定及归属、端内免登与停用失效、本人/他人/主管直接请求鉴权、通知任务回执和逐人结果、网页确认，以及超时未知不重发。当前未实施向真实人员试发，这些均不能在本地测试通过后自动标成已验收。
