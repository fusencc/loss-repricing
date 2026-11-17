# 2026-11-13 筛选来源、固定费用和库存接口

本项目规则版本为 `loss-local-history-web-sales-20261113-v3`；固定表内容版本独立保存。依据用户本轮五条批注及指定的 `D:\钉钉\BD项目流程跟进表V2.xlsx`。本次在WSL主仓库修改，默认仍为demo，真实提交关闭。

## 当前执行顺序

1. 当前一次毛利非负，本轮不调；保留现行一毛口径。
2. 查询本程序真实执行档案：确认生效未满72小时不调；未知提交持续锁定。
3. 销售网页七日销量：同店按ERP真实主SKU汇总变体七日销量，>=21件不调。
4. 同一具体在线子SKU选最小有效单件亏损，换成对应站点币，得到 `T0=P+Δ`。
5. 依据本次读取的海外库存选择固定表公式，`T=max(T0,完整利润保本价,一次毛利保本价)`；0%可以通过。
6. `d=P/R`，`Uraw=T/d`；向上进上传档位，再用 `U×d` 复核实际价和两类利润。

这里P是当前实际营销售价、R是当前含VAT零售价。除以d是把目标实际价反推到上传字段。例如P=50、R=100，补亏后T=55，d=.5，上传110后预期实际价55。不是把55再加一次VAT。费率公式中的VAT只按固定BD表的位置计算，不重复加到上传结果。

## 自有成功档案

`services/local_price_history.py`只读权威库中的live Batch/Item/Execution、VariantGuard及人工保护；`PricingService._enrich`的真实执行保护也只查本程序数据库。通用HTTP合同移除外部history_known/last_increase_at/pending_submission映射，采集后先保留历史未确认，再由服务读取本地库。成功需包含实际含税价格、实际生效时间或首次成功确认时间及live证据。受理不算生效，失败终态不占用未知锁。

当前本地权威库为 `runtime/live-history.db`。真实执行为0条是首次未曾运行的现状，不是构造成功样本；旧公司观察数据仍在数据库，但ORM及抓取/合并路径已删除，不参与判断。不清理用户历史原件。

## 销售网页七日来源

11/17更新：父商品列表只用于发现链接，销量取 `POST /Api/api-tiktok-sales/online/findProductVariants/{shopName}/{productId}` 的子SKU `sevenDays`。ERP官方 `queryGoodsSales` 的 `sku/mainSku` 只确认归属，不消费仓库销量。混挂其他主SKU的件数排除；同店同产品同sellerSkuId只计一次，同一子SKU不同链接正常相加。

从候选子SKU和主标签链接出发，批量确认ERP归属后，通过已实测的 `localSellerSkuStr` 反查同店其他链接，迭代发现关联规格。此范围不能证明穷尽ERP目录中完全未关联的其他规格。默认销售请求串行间隔2秒；缺页、重复、归属未知、空变体和限流不填0，能确认归属的坏销量只阻止相应主SKU。每个销量保留变体原件和ERP映射引用。

11/17只读样本：Tiktok-22236TH 的混挂链接总量246件，其中XXHG667为243件、GQHG18223为3件；反查后分别归入真实主SKU。

`sevenDays`没有返回精确窗口起止。规范输入为 `sales_basis=sales_system_web_sku_seven_days`、`sales_observed_at`，采集schema为 `sales-web-sku-seven-days-v2`；按北京时间要求本业务日数据，跨日分页不合并。此指标不声称是前七个完整自然日。旧父商品总量快照不能直接用于新口径判断，必须重新采集，不能只改版本标识。

## 固定表和公式

两份同名工作簿（含“(1)”副本）字节一致。SHA256为 `d8ec940c3762376c5c84e67b7bd9c91e8d16e9c2036afd43c2fc7b85c27666b3`，导入版本 `user-fixed-d8ec940c3762376c`。原始Excel不修改。

导入命令：

```bash
.venv/bin/python scripts/import_pricing_fees.py /mnt/d/钉钉/BD项目流程跟进表V2.xlsx --output runtime/fixed-pricing-fees.json
```

运行读取 `LR_FIXED_FEE_CONFIG_PATH`；未指定时用 `LR_RUNTIME_DIR/fixed-pricing-fees.json`。每次准备独立保存固定配置原件、哈希、版本及采集绑定。不自动重读Excel或在线改固定费率。11/16起TH直发VAT为独立在线商品输入，见[VAT来源规格](superpowers/specs/2026-11-16-th-vat-source-design.md)。

| 数据 | 固定来源 | 用法 |
|---|---|---|
| 直发公共费率 | 费率表 A2:E6 | MY、PH、TH、VN；TH的VAT文本不是数值 |
| 海外费率和固定费用 | 费率表 A12:U16 | 按站点分项；核对分项与合计，保留0.0001等数值 |
| 部门直发费率 | 费率表 A22:G38 | 按商品当前部门与站点精确选择 |
| 跨境运费 | 费率表 E52:G64中对应站点行 | 首重、首费、每10g续费；按商品当前重量进档 |
| 商品公开计划佣金 | BD项目 M列，按G/I/J/L列身份绑定 | 当前仅7行商品输入；未匹配不填0，不推广成全站默认 |
| TH直发VAT（11/16更新） | 本轮在线父商品taxVatRate及tkCategoryIdLeaf | 百分数；不增加请求，不消费旧V列；缺失待处理 |
| 库存、采购成本、头程、重量、现价 | 当前销售/平台查询 | 不导入表内过时的商品快照或公式缓存 |
| 汇率 | 当前公司配置 | 不使用固定表旧报价；补亏换汇和费用报价各留来源 |

海外完整利润对应BD项目AV：

```text
a = 1 - (资金+手续费+平台+退货+仓储+营销+其他+公开计划佣金)/100
b_CNY = (采购成本+头程+七项固定费用) × (1+VAT/100)
G(P) = a×P - b_CNY / (CNY每站点币报价)
```

直发完整利润对应AU/AE：

```text
物流站点币 = 首费 + ceil(max(重量g-首重g,0)/10g) × 续费
b_CNY = (采购成本+包材+仓库处理费+物流站点币×报价) × (1+VAT/100)
a = 1 - (手续费+平台佣金+退货+营销+其他+公开计划佣金)/100
G(P) = a×P - b_CNY / 报价
```

表中AD固定运费没有出现在AU公式中，目前配置均为0；若改成非0则要求明确公式，不能漏算。表内12%/12.5%目标利润不覆盖用户确认的0%保本规则。公式缓存中的错误/过期利润不导入。SG缺公共费率；TH直发缺类目VAT、任意商品缺公开计划佣金、未知成本或多海外仓归属不唯一时，相应商品待处理。

## 库存接口核验

1. 当前分流来源仍是销售变体 `POST /Api/api-tiktok-sales/online/findProductVariants/{shopName}/{productId}`：`overseasStockSum`和`overseasStockWarehouseInfo`。原件含分仓数量、采购成本和头程。本轮初次请求HTTP500/resp_code9999，上游重试超限；19:07新连接复测HTTP200/resp_code0，XXHG667-A1库存563，分仓数量563，采购成本0.6978、头程0.0277。故障时仍保留未知，不改成0库存。
2. 官方ERP `POST /open/inventory/overseas/listDetail`已于北京时间18:59重新实测HTTP200/code200/success=true。当前部署base本身为`.../open`，完整路径为`.../open/open/inventory/overseas/listDetail`，不能少拼一层。
3. 合并销售员的样本返回7仓。同一泰国小货仓：`bizActualQty=687`、`bizUsableQty=650`、`realWarehouseInventory=687`、`entityActualQty=0`；不能把这些数互换。未合并样本此前total138但第一页100条时hasNext=false，接入必须按total/实际条数核对分页。合并结果可能丢失集团/销售员归属。

因此官方接口已经找到并验证可访问，但尚未替换当前库存分流来源。还需明确取“库存中心”“可发”“真仓”中的哪一项，以及ERP仓到在线仓的映射、成本单位/更新时间。PLM日报整批完成触发仍未接入。

只读实测原件及摘要在 `runtime/user-policy-20261113/`；先前官方目录/分页证据在 `runtime/official-interface-check-20261113/`。业务原件不进入Git。

## 交付边界

本次固定配置导入和接口只读抽样均已执行；当前实际全量提价仍未运行。最终全量后端751项通过（452.55秒）；前端生产构建、compileall与git diff --check通过。仅1条Starlette/AnyIO既有依赖弃用警告。独立复查发现的两项异常隔离/HTTP历史依赖问题均以失败测试复现并修复，152项相关回归通过。完整结果见runtime/user-policy-20261113/final-tests.log及实施计划；不以测试通过替代真实平台上传、折扣保持及实际价格生效验收。

公开计划佣金10%目前只绑定表内7条商品；是否扩展为全商品统一固定值，已向用户询问，尚未收到范围确认。本轮未作该扩展。

11/17完整性边界：当前发现范围查询成功标记为 `discovered_scope_complete`，汇总件数仅作参考；因未证明全ERP主SKU规格覆盖，`complete=false`，下游保留待处理，不自动放行调价。不能只将完整性标记改为true。
