# ERP只读数据采集

2026-11-05实测网页登录成功，使用项目已有 `.env` 中的独立账号密码。
网页登录与内部开放接口分别认证，令牌不会跨服务复用。

管理员登录工作台后可请求 `GET /api/sources/erp/sample?limit=5`。
每组抽取第一页1至10条，并逐订单补齐明细；返回明确标记 `sample_only=true`、
`complete=false`、`pricing_ready=false`。这份结果不作为可执行定价快照。

也可在项目根目录运行以下PowerShell命令，白名单业务字段输出到被Git忽略的运行目录。

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python -m app.capture_erp --limit 5
```

|来源|已核实参数与字段|
|---|---|
|`/Api/deficit/list`|status=4可配货、3待处理，platform、bloc，grossprofit、humancost、difference|
|`/Api/order/list`|status=4/3，ptid、blocs，两个状态均显式携带jzfh="1"|
|`/Api/order/findDetail/{orderid}`|detaillist中的sku、qty、currency、exchangeRate、成本、仓库等|

`LR_ERP_CAPTURE_PLATFORM`、`LR_ERP_CAPTURE_PLATFORM_ID` 和 `LR_ERP_CAPTURE_BLOC`
默认使用已核实的TikTok集团二部范围；修改时需同时核实名称与ID对应关系。
不隐含添加差额小于等于0的balance筛选，禁止发货记录也可能有正毛利。
单组或订单明细失败会隔离并显示错误，不能把失败当作空记录或完整采集。

金额保持来源字段名及十进制字符串。currency不能被推断为所有金额字段的币种，
sellPrice与originSellPrice实测存在汇率换算关系。itempriceinclvat实测为标识样式，
不作为含税价格。订单毛利与扣人力后的差额尚需确定选用口径。
多SKU亏损分摊、业务更新时间、在线子SKU映射、同店同站主SKU七完整日销量尚待确认。
开放接口商品当前成本与历史订单成本存在差异，不能直接覆盖。

## 只读诊断报告（2026-11-05）

管理员进入网页「运行配置 → ERP 数据获取验证」，选择每组1至10条，点击「运行真实只读验证」。
页面加载、演示批次和定时任务均不会自动触发此验证；即使当前为demo模式，该按钮仍明确访问真实ERP。
列表和明细串行读取，可能需要数分钟；默认每组2条，不自动重试。

新增 `GET /api/sources/erp/validation?limit=2`，沿用管理员鉴权，仅返回诊断报告；
原 `/sample` 响应新增 `validation`，仍保留原有白名单样本。
`adapters/erp_validation.py` 复用本轮结果汇总，不新增外部请求，不写批次、执行意图、商品锁或定价快照。

报告分别记录：

- 登录成功、四组列表读取成功/失败、实际筛选条件和样本出现的店铺ID。
- 接口自报总数、实际样本数、去重后的样本订单标识和覆盖状态：仅第一页、总数未知、计数矛盾、自报零条或已观察自报条数。
- 去重后的订单明细读取成功/失败数量；缺失订单标识、空明细和原始sku/currency/qty问题。
- HTTP 401为认证拒绝，403为请求被拒绝；其他HTTP/网络/格式问题不推断为无权限。
  非成功数字业务码单独保留，但没有已确认合同的业务码不推断权限含义，也不回传上游msg。
- 待确认的数据语义、公司公式、汇率与折扣行为，以及下一步人工导出核对要求。

`total` 仅接受非负整数，其他值显示未知，不阻止独立明细读取，也不原样下发异常对象。
返回零条不等于无权限；样本未出现某店铺不等于不可见；样本数等于total也不证明公司全量完整。
`qty`为正数、`currency`为三位大写代码，仅说明原始字段可检查，不证明该币种是毛利币种、
该数量是亏损对应数量，或SKU已完成唯一映射。

所有报告固定 `sample_only=true`、`complete=false`、`pricing_ready=false`、
`permission_scope_verified=false`；不会修改真实提交开关、试点、公式或任何合同确认标记。
网页登录失败则接口返回脱敏503，不生成登录成功报告。

页面支持下载报告；内容含筛选范围和业务标识，仅在受控位置保存，不提交Git。
命令行可仅保存诊断信息而不保存原始明细：

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python -m app.capture_erp --limit 2 --report-only
```

账号完整范围仍需用批准的店铺清单及同账号、同筛选人工导出独立对照。
本次未实现全量分页、自动人工导出比对、在线多来源合并或公司费用模型校准，不能将诊断报告作为上线验收。

已有 `/api/sources/erp/rates` 保持公司开放接口汇率读取。
四组候选采集不依赖该汇率的推断方向，不调用价格提交接口。
