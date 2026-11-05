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

已有 `/api/sources/erp/rates` 保持公司开放接口汇率读取。
四组候选采集不依赖该汇率的推断方向，不调用价格提交接口。
