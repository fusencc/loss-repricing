# 2026-11-13 筛选来源、固定费用和库存接口

本项目规则版本为 `loss-online-margin-1pct-20261120-v7`；固定表内容版本独立保存。依据用户本轮五条批注及指定的 `D:\钉钉\BD项目流程跟进表V2.xlsx`。本次在WSL主仓库修改，默认仍为demo，真实提交关闭。

## 当前执行顺序

1. 亏损候选直接使用 ERP 下载的「修正一次毛利」负金额，零或正数不作为亏损，缺失或冲突保持异常；不再按重算的现价毛利率排除候选。
2. 查询官方开放调价日志：滚动72小时内type=2记录数大于0不调，明确0通过，未知待处理；本程序未知提交持续锁定。
3. 同一具体在线子SKU选最小有效单件亏损，换成对应站点币，得到 `T0=P+Δ`。
4. 依据本次读取的海外库存选择固定表公式，`T=max(T0,完整利润保本价)`；1%可以通过。
5. `d=P/R`，`Uraw=T/d`；向上进上传档位，再用 `U×d` 复核实际价和所选仓配完整在线利润。

这里P是当前实际营销售价、R是当前含VAT零售价。除以d是把目标实际价反推到上传字段。例如P=50、R=100，补亏后T=55，d=.5，上传110后预期实际价55。不是把55再加一次VAT。费率公式中的VAT只按固定BD表的位置计算，不重复加到上传结果。

## 开放日志保护与本地执行档案

11/17用户指定`getOperationLogCountBySellerSkuIdsAndTypeAndCompleteTime`为近三天判断来源。按子商品ID查询北京时间完成时间范围，覆盖官方日志中的其他操作者调价；未确认成功状态过滤，不把记录数量当成生效证据。真实服务刷新、ERP准备及自动提交前核对均使用该接口。详细字段、异常处理及秒级边界限制见[官方调价日志](official-price-log-integration.md)。

`services/local_price_history.py`只读权威库中的live Batch/Item/Execution、VariantGuard及人工保护，用于未完成执行防重和人工保护。真实模式不再以本地生效时间做72小时冷却，也不回退到本地库证明没有外部记录。实际生效核验与价格存档继续独立留证；不修改旧批次或清理用户历史原件。

## 固定表和公式

两份同名工作簿（含“(1)”副本）字节一致。SHA256为 `d8ec940c3762376c5c84e67b7bd9c91e8d16e9c2036afd43c2fc7b85c27666b3`，当前导入版本 `user-fixed-no-public-commission-v2-d8ec940c3762376c`（11/19移除公开计划佣金）。原始Excel不修改。

导入命令：

```bash
# 当前费用与物流由每日销售系统快照提供，不再执行旧表导入。
```

11/19按用户要求核对陈波当天消息后，真实运行的费率改为每轮从销售系统读取，合同为`sales-current-pricing-fees-v1`。常规及人工入口统一使用海外仓固定费率、直发公共费用及部门兜底费率，不读取个人/店铺覆盖，不回退旧BD表费率。原件、哈希、修改时间和本轮观察时间存入`current-fee-sources`。`LR_FIXED_FEE_CONFIG_PATH`（默认`LR_RUNTIME_DIR/fixed-pricing-fees.json`）仅继续提供物流价卡及其独立版本。TH直发VAT仍复用当前父商品，见[VAT来源规格](superpowers/specs/2026-11-16-th-vat-source-design.md)。

| 数据 | 固定来源 | 用法 |
|---|---|---|
| 直发公共费率 | 当前`getPublicRatesByShopName` | 按店铺解析公共费用；明确类型、单位和币种；TH类目VAT另取 |
| 海外费率和固定费用 | 当前`supplierrateoversea/findByPage` | 当前集团固定配置，逐站点唯一；核对分项与合计，保留0.0001等数值 |
| 部门直发费率 | 当前`supplierrate/findByPage` | `authType=2`，按商品部门与站点精确选择；多候选待处理 |
| 跨境运费 | 费率表 E52:G64中对应站点行 | 首重、首费、每10g续费；按商品当前重量进档 |
| TH直发VAT（11/16更新） | 本轮在线父商品taxVatRate及tkCategoryIdLeaf | 百分数；不增加请求，不消费旧V列；缺失待处理 |
| 库存、采购成本、头程、重量、现价 | 当前销售/平台查询 | 不导入表内过时的商品快照或公式缓存 |
| 汇率 | 当前公司配置 | 不使用固定表旧报价；补亏换汇和费用报价各留来源 |

海外完整利润对应BD项目AV：

```text
a = 1 - (资金+手续费+平台+退货+仓储+营销+其他)/100
b_CNY = (采购成本+头程+七项固定费用) × (1+VAT/100)
G(P) = a×P - b_CNY / (CNY每站点币报价)
```

直发完整利润对应AU/AE：

```text
物流站点币 = 首费 + ceil(max(重量g-首重g,0)/10g) × 续费
b_CNY = (采购成本+包材+仓库处理费+物流站点币×报价) × (1+VAT/100)
a = 1 - (手续费+平台佣金+退货+营销+其他)/100
G(P) = a×P - b_CNY / 报价
```

表中AD固定运费没有出现在AU公式中，目前配置均为0；若改成非0则要求明确公式，不能漏算。表内12%/12.5%目标利润不覆盖用户确认的1%最低利润率规则。公式缓存中的错误/过期利润不导入。SG缺公共费率；TH直发缺类目VAT、未知成本或多海外仓归属不唯一时，相应商品待处理。

## 库存接口核验

1. 当前分流来源仍是销售变体 `POST /Api/api-tiktok-sales/online/findProductVariants/{shopName}/{productId}`：`overseasStockSum`和`overseasStockWarehouseInfo`。原件含分仓数量、采购成本和头程。本轮初次请求HTTP500/resp_code9999，上游重试超限；19:07新连接复测HTTP200/resp_code0，XXHG667-A1库存563，分仓数量563，采购成本0.6978、头程0.0277。故障时仍保留未知，不改成0库存。
2. 官方ERP `POST /open/inventory/overseas/listDetail`已于北京时间18:59重新实测HTTP200/code200/success=true。当前部署base本身为`.../open`，完整路径为`.../open/open/inventory/overseas/listDetail`，不能少拼一层。
3. 合并销售员的样本返回7仓。同一泰国小货仓：`bizActualQty=687`、`bizUsableQty=650`、`realWarehouseInventory=687`、`entityActualQty=0`；不能把这些数互换。未合并样本此前total138但第一页100条时hasNext=false，接入必须按total/实际条数核对分页。合并结果可能丢失集团/销售员归属。

因此官方接口已经找到并验证可访问，但尚未替换当前库存分流来源。还需明确取“库存中心”“可发”“真仓”中的哪一项，以及ERP仓到在线仓的映射、成本单位/更新时间。PLM日报整批完成触发仍未接入。

只读实测原件及摘要在 `runtime/user-policy-20261113/`；先前官方目录/分页证据在 `runtime/official-interface-check-20261113/`。业务原件不进入Git。

## 交付边界

本次固定配置导入和接口只读抽样均已执行；当前实际全量提价仍未运行。最终全量后端751项通过（452.55秒）；前端生产构建、compileall与git diff --check通过。仅1条Starlette/AnyIO既有依赖弃用警告。独立复查发现的两项异常隔离/HTTP历史依赖问题均以失败测试复现并修复，152项相关回归通过。完整结果见runtime/user-policy-20261113/final-tests.log及实施计划；不以测试通过替代真实平台上传、折扣保持及实际价格生效验收。

2026-11-19用户明确移除公开计划佣金。导入器只读取费率表配置，不导入BD商品M列；不限制商品名单，不因缺少公开计划佣金阻断。直发及海外仓模型均不额外扣这项费用，平台基础佣金保留。固定配置合同为`user-fixed-pricing-fees-v2`；现有运行配置须重新导入，历史快照保留原件。
