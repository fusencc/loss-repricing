# 数据合同证据与联调缺口

核查日期：2026-11-05。本文只读核对本地脱敏源码、历史日志、11月4日静态审计与接口核查报告；没有向生产系统发请求，没有运行原程序或宏。接口路径仅列相对路径，不保存服务地址、账号、凭据、下载地址或原始业务记录。

## 证据分级与使用原则

- **源码已见**：只证明旧客户端使用过该字段，不能代表当前服务的完整合同或成功响应。
- **历史日志已见**：只证明当时该批次发生过该结果，不代表今天权限、字段、状态含义未变。
- **静态公式已见**：只证明工作簿保存的公式，不能代表参数今天有效或公司已批准本期口径。
- **11月4日只读验证**：销售认证 HTTP 200 且返回 token；一次历史查询超时。没有完成业务查询、导出、上传或实际价验证。

内部规范字段可自行命名，但适配器必须保存来源字段映射及合同版本，不能把内部字段名声称为公司接口字段。未知合同应阻断对应真实执行。

## 1. ERP订单与四份候选

证据：`C:/Users/Administrator/Desktop/AIBP公共空间经验库/fba-main/dongnanya_html_baobiao/dashboard_sync.py`。

| 接口 | 源码中实际请求 | 源码中实际读取响应 | 已知限制 |
|---|---|---|---|
| POST `/Api/order/list` | JSON：`storageType="海外仓"`、`time="r.express_time"`、`starttime`、`endtime`、`page`、`limit`；可选 `storageEntityname` | `data.total`、`data.pages`、`data.records[]`；记录读取 `storageEntityname`、`orderid`、`platform`、`ordertype` | 是已发货海外仓统计逻辑，不是四份亏损候选合同 |
| POST `/Api/order/findByOrderId` | JSON：`orderid` | `data.ordDetailList[]` 中 `sku`，为空时回退 `skuid` | 旧代码只抽SKU，没有证明亏损、数量、币种与明细键 |

出处：源码137—146、1195—1238、1418—1449行。分页边界可能重复订单，旧代码按订单号去重；本期必须保留明细，不可据此按订单号丢掉不同子SKU。

本期四份候选为“所有订单→亏损订单”和“所有订单→禁止发货”，各取“可配货、待处理”。当前未核实的内容：

1. 对应接口及筛选字段/枚举，四份导出的同一快照/更新时间含义。
2. 稳定订单明细键，订单号、明细号、子SKU、产品ID、店铺、站点、仓配的准确字段。
3. 一次毛利额是订单、明细还是单件；币种；件数是明细数量还是整单总数；金额符号和是否已分摊。
4. 多SKU订单费用如何归属，不能用整单亏损除总件数后随意分给不同SKU。
5. 新店/新品、禁止发货及特殊保护是否由查询条件或导出界面预先过滤。

`/Api/deficit/list`仅在前期资料中作为线索出现，没有本次可用的完整请求/响应合同。不得自行拼字段启用。

## 2. 财务导出不等于亏损候选

罗迪`亏损调价-获取数据.py`使用 GET `/api/SendDownload/selectByPrimaryKey`触发、GET `/api/sendSkuDownload/getReportFanceTask`轮询。旧流程按昨日发货窗口和`orderOrSku="sku"`取数。

触发响应读取`code`、`message`；轮询读取`result.records[]`内`status`、`href`、`createOn`、`menu`，挑最新记录。没有证明提交返回ID与下载任务绑定。本期可复用导出适配方式，不能直接把此数据认作四候选。`href`属于访问地址，不进Git/普通日志。

## 3. 销售认证、在线导出与历史

源码根目录（下文“罗迪脱敏源码”）：`C:/Users/Administrator/Documents/ChatGPT/公司信息百科全书/output/亏损提价接口评估_20261103/脱敏代码/`。

### 认证

POST `/Api/api-auth/oauth/user/token`：旧代码在header传`client_id`、`client_secret`，form传`username`、`password`，读取顶层`access_token`，后续使用`Authorization: Bearer ...`。这里只记录字段名。源码未证明有效期与refresh合同；禁止把完整响应、认证header和异常中的敏感数据写日志。

### 在线子SKU导出

`营销价格导出.py:102—159`：

- POST `/Api/api-tiktok-sales/online/export`，JSON字段为`blocNames`、`productIdStr`、`exportType`（旧值2）、`exportField`。
- 旧`exportField`明确请求：产品ID、商品ID、平台子sku、本地子sku、税前价、平台类目链（英文）、折扣。
- 只检查顶层`resp_code==0`及`resp_msg`，没有保存提交返回的任务ID；不能据此声称服务不返回ID。
- POST `/Api/api-tiktok-sales/exportTask/findByPage`，请求`page=1,limit=10`，读取`datas.data[]`。
- 任务对象实际读取`id`、`operator`、`taskType`、`createTime`、`completeTime`、`status`、`excelName`、`excelUrl`、`failReason`。旧代码用操作人、任务类型、创建时间选任务；旧常量`taskType=1`、完成状态3、失败状态4。
- 这些是**导出任务**枚举，不能用于改价任务。必须核对本次提交返回值与轮询对象`id`的关系，不能继续按“最新任务”配对。

旧整合代码`完整信息整合.py:99—106`只保留部分列，丢失已请求的折扣。缺折扣是已知客户端问题，不能说接口不提供折扣。当前营销价、含VAT零售价、在线正常利润、活动锁价、更新时间、明确仓配以及店铺/站点关联，在该导出请求中尚未完整证明。

### 调价历史

`筛选ITEMID.py:110—159`：POST `/Api/api-tiktok-sales/operationLog/findByPage`。

- JSON：`departmentName`、`createTimeStart`、`createTimeEnd`、`type`、`status`（旧值2）、`productIds`、`page`、`limit`。
- 旧代码只读取`datas.count`，只查首页；没有解析记录数组或逐条执行详情。
- `type`与`status`的业务含义、店铺范围、SKU范围、手动改价覆盖、生效时间、完整分页、超时后的查询能力均需确认。
- 旧函数把HTTP400、超时、响应异常与无记录都返回False。新程序必须分为“有记录/确证无记录/未知”，未知不能放行。

## 4. 上传Excel与返回结构

原始空模板证据：`C:/Users/Administrator/Desktop/需求梳理文档/.pmo_analysis/01_ERP页面核对_2026-10-14/表格调价模板_原始下载.xlsx`，`DATA-0!A1:I1`。

| 列 | 原模板字段 |
|---|---|
| A | 店铺名 |
| B | 产品ID |
| C | 商品ID |
| D | 税前价格 |
| E | 税后价格 |
| F | 海外仓税前价格 |
| G | 直发仓税前价格 |
| H | 海外仓税后价格 |
| I | 直发仓税后价格 |

旧代码`完整信息整合.py:296—318`实际只按位置填前四列，前三列内部表头带“（必填）”，D列使用`税前价*1.01`。这证明旧上传文件生成路径，不代表本期策略或含税合同正确。ID强制按字符串写入可保留；列应按已确认模板表头映射。

`完整信息整合.py:339—355`：POST `/Api/api-tiktok-sales/operationLog/priceUpload`，multipart字段名`file`，文件扩展名xlsx，MIME为`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，使用Bearer认证。源码读取顶层`resp_code`、`resp_msg`。

**已见的行级结果是消息字符串，尚未发现结构化逐行JSON合同。** 历史日志示例：

```text
上传成功：上传完成,第16行数据不存在在线列表;第14行数据不存在在线列表;...
```

出处：脱敏日志`亏损调价_20260914_092001.log:311`；10月5日批次第320行同类错误26条。代码仅因`resp_code==0`就打印“上传成功”。历史10批1,102条模板记录中，5批累计35条该错误。

因此可针对已见消息识别确定的行错误，但还必须核实：行号是否含表头、该行属于源文件还是服务转换文件、其他错误类型、是否部分受理、是否有任务ID/结构化明细接口。没有列入错误字符串的行只能认作“受理结果待核验”，不能认作已生效。内部`row_results`结构只能标作内部规范化结果，不得假设真实API返回同名对象。响应未知不自动重传。

税后列确实存在，但当前`priceUpload`对本土/跨境和不同仓配列的解释尚未验证；不能把同一价格同时填入多个税价列。新SOP要求含VAT上传口径，必须与试点合同一致才启用。

## 5. 最新成功价与当前有效价

`提价检查接口.py:144—178`：POST `/Api/tiktok-schedule-server/aibp/getLatestSuccessPrice`，JSON为`shopName`、`productId`、`sellerSkuId`；`resp_code==0`时直接返回`datas`，旧代码将其当价格值。

该源码**没有读取**当前平台获取时间、价格类型、活动/锁价、仓配、任务关联或生效时间。因此不能把`datas`直接映射为“当前有效营销价”，也不能用它独自完成本期核验。另需确认源码参数`sellerSkuId`究竟与模板“商品ID”如何对应。

可接受的后续证据是已有查询/在线导出的真实字段，能确认同一复合商品身份、同币种、同仓配、税价/营销口径及足够新的取价时间。无需先假定公司必须新增接口。价格回读失败保持待核验；“最新成功”这个名字不是生效证明。

## 6. normal_profit参数的真实公式依据

证据：`C:/Users/Administrator/Desktop/2026-11_亏损提价项目/output/技术充分性复核_20261104/提价工具静态审计_脱敏.json`的`template_formula_evidence.U2`、`template_snapshot.fee_parameter_headers`。原工作簿为同项目`提价案例/批量模板.xlsm`，数据表U列及W1:AL6。

U2保存的公式是“现价利润率”，可在**不先取整**的分子层面代数整理为：

```text
raw_profit(P) = P * revenue_factor - fixed_cost
raw_margin(P) = raw_profit(P) / P
旧U列显示值 = ROUNDDOWN(raw_margin(P), 3)
```

这里只证明旧工作簿公式可以写成仿射形式，**不证明它已经等于本期normal_profit**。

变量按原公式定义：`P=F行营销价`，`E=成本（RMB）`，`x=AC站点行汇率`，`B=AB站点行固定费合计`，`k=AK站点行费率合计/100`。原公式把RMB费用除以x转为站点币，故x在这套公式中的量纲为RMB/站点币；正式接入必须与实际汇率字段方向对账，不可凭字段名“汇率”假设。

| 站点 | 站点参数行 | 旧revenue_factor | 旧fixed_cost（站点币） |
|---|---:|---|---|
| MY | 2 | `(1-k)/(1+AL2/100)` | `(E+AB2)/AC2 + L_MY` |
| PH | 3 | `1-k`，U公式没有除VAT | `(E+AB3)/AC3 + L_PH` |
| SG | 4 | `(1-k)/(1+AL4/100)` | `(E+AB4)/AC4 + L_SG` |
| TH | 5 | `(1-k)/(1+H行/100)`，使用数据行H，非AL5 | `(E+AB5)/AC5 + L_TH` |
| VN | 6 | `(1-k)/(1+AL6/100)` | `(E+AB6)/AC6 + (6.5/1000*w+1)/AC6` |

上述`k`分别取该站点行。`w=MAXIFS(D:D,B:B,本行产品ID)`，是同产品ID最大重量，并非天然是当前子SKU重量；不能不经验证直接沿用。按U2原式：

```text
L_MY = ROUNDUP(max(w,20)/10,0)*0.15
L_PH = 15                         (w<=20)
       CEILING(w-10,10)/10*4.5+10.5 (w>20)
L_SG = 0.98                                      (max(w,20)<41)
       CEILING(max(w,20)-40,10)/10*0.15+0.98       (其余)
L_TH = ROUNDUP(max(w,20)/10,0)*1
```

这些物流常量只记录旧公式证据，不是今日批准费率。重量单位、物流适用仓配、费用生效日仍须确认。

### 不能忽略的本期差异

- `AB=SUM(Y:AA)`：Y包材费（RMB）、Z人力成本预估（RMB）、AA各站点平台支持/订单处理/基础建设费用（RMB）。
- `AK=SUM(AD:AJ)`：交易手续费、佣金率、资金成本费、包邮率、退款率、**AI营销费（联盟+广告）**、其他费率。
- **旧U公式包含AI营销与广告费，本期在线正常一次毛利判定要求排除广告与营销费。不能直接把旧AK传给normal_profit。** 若公司确认这套费用分解完整且AI是应排除的全部费用，可据已确认配置重建`k_normal=(AD+AE+AF+AG+AH+AJ)/100`；这是候选适配方式，不是已验收公司合同。还须核实其他列是否含重复营销费用、退款/物流与VAT税基是否一致。
- 旧U显示取三位小数会掩盖小幅负值。自动执行应使用原始Decimal金额判断，显示取整与执行阈值分离，不能用已取整U反推出精确固定成本。
- 不能只凭一个当前利润率点唯一确定`revenue_factor`和`fixed_cost`；一个点有两个未知量。必须取得批准的费率分项/固定费用，或公司明确的等价公式与独立核对样本。
- 两行已复算样本均为MY，证明旧模板能复现14.64/10.20上传结果，未证明PH/SG/TH/VN正常利润模型，更未证明今天配置。
- 本期目标价仍按现行直接补亏规则产生；normal_profit只承担复核。复核不通过转待处理，不能用反解盈亏平衡价或旧U1=15%目标利润自动追加涨价。

建议真实合同至少保存`profit_model_version`、税/费用分项及单位、汇率方向与日期、配置来源/更新时间、适用站点/仓配、分项排除说明、原价与目标价的核对结果。只有经核对的适配器才生成规范化`revenue_factor`、`fixed_cost`；缺项不设零、不设factor=1。

## 7. 当前内部合同要求与真实证据的区别

本次检查补强了以下内部约束，均不代表公司接口已有同名字段：

- 站点合同必须填写 `warehouses`，明确哪些来源仓配编码已经验证可使用该含税上传列及费用版本。空列表或未覆盖仓配不进入提交。还须核实本土/跨境等实际适用维度，不能把一个站点配置无限外推。
- 查价合同必须映射 `currency`，有效价与站点币不一致或缺失时保持未知；不能只凭站点名称推断金额单位，也不自动把回读价换汇后当作正确。
- 查价合同必须给出正整数 `max_age_seconds`，含义是核验时允许来源价格观测滞后的最大秒数。值必须根据真实接口的更新时间语义确认；测试中的 300 秒只是合成合同，不是已批准统一阈值。过期观测不能证明当前价格。
- 精确生效时间缺失时，应以本程序首次确认时间起算 72 小时，并与来源观测时间分别留证。旧查询记录的时间不能冒充首次确认时间。
- 规范快照的销量完整、历史已知、整单分摊可靠性在缺失时均按未确认处理。已有文件若依赖原默认 true，需补充真实证据后显式填写，不可只为恢复“通过”而批量填 true。

真实证据包至少记录：负责确认的人、确认日期、请求筛选及字段语义、金额单位和粒度、适用店铺/站点/仓配、源时间含义、版本、生效期间、脱敏样本引用及对账结果。应用当前只消费其中部分规范字段和版本；完整业务确认材料仍需受控维护，不能声称 JSON 配置自动验证了证据真实性。

## 8. 下一步最小联调清单

1. 取得同一时间的四候选脱敏样本，确认筛选条件、数量/币种/亏损粒度与明细键；与人工导出逐项对账。
2. 取得在线导出一次真实提交与对应任务对象，确认任务ID绑定、复合身份、现价/折扣/活动/更新时间及费用模型来源。
3. 明确当前模板税后及仓配列合同，以试点商品覆盖有/无折扣；保存本地文件行号映射，验证服务报错行号。
4. 验证上传受理、逐行结果/任务查询和当前实际有效价的关联；超时后先查询，不以重传试探结果。
5. 用公司同口径利润样本验证每个试点站点/仓配的参数映射。任一合同未确认，仅该范围关闭真实执行。

## 9. 服务器连接的本地非敏感指针

- SSH配置：`C:/Users/Administrator/.ssh/config`，存在别名`aliyun-rocky`，配置端口2323；本次只报告别名/端口，未读私钥。该配置没有IdentityFile，也没有ProxyCommand；别名存在不等于本次已验证可无交互连接。
- 现有连接参数的参考位置：`D:/projects/ad-recharge-mvp/scripts/sync-github-server.ps1`。参数名含Server、User、Port、IdentityFile、ServerDirectory；它使用BatchMode与StrictHostKeyChecking。密钥文件位置指针为`C:/Users/Administrator/.ssh/ad_recharge_sync`，不复制、不读取内容。
- 该脚本执行的是预算仓库同步，不能运行它来部署本项目。`D:/projects/ad-recharge-mvp/deploy/server-deploy.sh`同样仅可参考，不能复用目标目录执行。
- 若后续获准连接，应在本项目独立发布目录/Compose项目中工作，先只读核实主机身份与资源；不沿用预算业务数据库或应用凭据。本文没有执行连接或部署。

## 本地证据索引

1. `C:/Users/Administrator/Desktop/2026-11_亏损提价项目/output/自动化方向与现有接口服务器核查_20261104.txt`。
2. `C:/Users/Administrator/Desktop/2026-11_亏损提价项目/output/技术充分性复核_20261104/现有接口与提价工具是否足够_技术结论.txt`。
3. 同上目录`提价工具静态审计_脱敏.json`、`只读接口验证结果.json`。
4. `C:/Users/Administrator/Documents/ChatGPT/公司信息百科全书/output/亏损提价接口评估_20261103/日志核查统计.json`及脱敏代码/脱敏日志。

以上均为本地历史/静态证据。上线完成、当前接口可用、价格已生效等结论必须由后续实际验证单独建立。

## 2026-11-05 汇率只读接入证据补充

用户提供内部开放接口登录/汇率查询文档，本会话实际登录查询成功：业务 success=true、code=200，data 为 currency/rate/rateDescr 列表。文档测试环境地址直接查询返回“服务未找到”；项目现有配置的公司ERP地址登录和查询成功。配置地址不因此宣称为已验收生产环境。

实现：`backend/app/adapters/erp_rates.py`，合同版本 `erp-rates-readonly-v1`；管理员API与设置页只读查询已实现。data.rate 的方向按数值推断为CNY/外币单位，仍未确认为公司最终核算口径；data 无生效日期，不使用 now 冒充汇率日期。原始业务响应及认证信息未写入Git。查询可用不等于真实自动定价可用，其他数据源和执行合同继续独立验收。

## 2026-11-05 销售系统账号认证实测与自动取证

用户授权使用本人业务账号测试登录与凭证更新，并明确禁止真实调价。新增 `adapters/sales_auth.py`、`app.probe_sales_auth`；`HttpAdapter`在vendor来源与已确认销售服务地址一致时自动获取有效Token。账号密码仅配置在本项目被Git忽略的.env，令牌在进程内缓存，不写业务数据库/日志/Git。配置名 `LR_VENDOR_API_URL`、`LR_VENDOR_USERNAME`、`LR_VENDOR_PASSWORD`，保留原静态 `LR_VENDOR_TOKEN` 兼容来源合同。

- 官方提供的YApi basic-server/21040发布POST `/api-auth/oauth/user/token`，入口经销售系统网关`/Api`前缀调用。请求header包含文档指定客户端参数；具体值及业务凭据不记录于本规范。
- **文档与实测差异**：文档标application/json；本账号JSON请求HTTP400，旧源码已见的form编码请求HTTP200，返回顶层access_token、token_type、expires_in、refresh_token、scope。实现固定使用实测成功的form，不在运行中尝试多个编码。
- 初次expires_in=179999秒（49小时59分59秒），scope=app；再次登录取证成功，剩余有效期递减，服务可能复用仍有效的Token，不宣称每次登录均换发不同Token或重置有效期。
- 有效期使用从请求开始计时的monotonic时间；到期前最多60秒重新登录，短寿命令牌预留10%。重新登录失败不返回旧令牌。刷新接口未发布，未猜测grant_type/刷新路径，未使用refresh_token做真实刷新测试；当前更新方式为重新调用已实测登录接口。
- 自动认证只解决身份凭证生命周期，不改变来源合同、公司费用模型、店铺授权、试点或真实执行开关。scope=app不能证明priceUpload权限。所有业务请求（尤其提交）失败后不因换Token而自动重发。
- 本次真实测试仅调用认证接口，未上传文件、未调价、未调用修改图片或其他写业务接口。首次及后续探测均未证明调价权限或平台生效。
