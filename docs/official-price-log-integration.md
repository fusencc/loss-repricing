# 官方调价日志接入与价格存档

## 入口与范围

`backend/app/adapters/official_price_logs.py`提供可复用的单目标只读采集函数
`capture_official_price_logs(settings, key, shop_name, business_date, directory, client=None)`。
认证只使用本项目SalesAuth；支持注入HTTP客户端作离线测试。接口如下：

| 查询 | POST路径 | 请求口径 |
|---|---|---|
| 操作日志数量 | `/Api/tiktok-schedule-server/aibp/getOperationLogCountBySellerSkuIdsAndTypeAndCompleteTime` | sellerSkuIds使用具体子商品ID，type=2；按明确北京时间完成日期查询 |
| 最近成功价 | `/Api/tiktok-schedule-server/aibp/getLatestSuccessPrice` | shopName、productId、sellerSkuId |

日归档函数一次查一个目标；近期保护函数按候选子商品ID批量查询，不进行全店扫描、日志导出或调价。统计接口的成功状态过滤尚未
确认；最新成功价只有金额，没有币种、任务ID或生效时间。不能用count>0或单个金额替代
任务成功回执与当前价核验。两个查询独立留证，一个失败不把另一个有效结果抹掉。

状态包括ok、not_found、business_error、transport_error、invalid_response。失败/缺失为null，
只有明确返回本SKU count=0才是零条。HTTP金额直接用Decimal解析，输出十进制文本。
原始响应哈希和核对后的响应字段归档，认证头、凭据及任意错误正文不落盘。

## 四种价格

`build_price_record`生成`execution-price-record-v1`，`prices`内有四个固定名称：

| 字段 | 含义 | 来源 |
|---|---|---|
| uploaded_tax_inclusive | 本程序实际提交的含税上传价 | 原Execution.request.upload_price |
| tax_exclusive | 该调价任务的税前价 | 已绑定operationLog.priceNew，与平台请求回执核对 |
| actual_tax_inclusive | 回读时的平台实际含税零售价 | 平台sku.price.sale_price，并与公司priceIncludeVat一致 |
| marketing | 回读时的营销实际价 | 公司当前变体marketPrice |

每项包含amount、currency、currency_source、source、source_field、observed_at和time_basis。
上传项另存原提交意图的submitted_at。observed_at是本次观察时间，不冒充平台源更新时间
或历史生效时间；原确认时间继续在Execution.evidence.confirmed_at/effective_at中保留。

记录同时保存execution_id、完整商品key、店名、任务ID/状态/操作人/创建及完成时间、
平台税前回读、物流费用、折扣、含税回读与上传价差、官方查询和比较结果。
官方最近成功金额只作比较：matches_log_tax_exclusive表示本次金额等于日志税前价，
不把官方接口没有返回的任务ID或币种时间补造出来。价格不同就分别保存，不覆盖。

## 正常执行与补档

真实人工执行的ManualLiveAdapter.lookup在取得成功任务及当前价证据后调用官方查询，
通过LookupResult.price_record交给PricingService，写入Execution.evidence.price_record。
四价JSON同时存入本次执行目录。原actual_price继续表示营销实际价，公式和核验容限不变。
官方接口失败会独立记录状态；不会把这次查询失败当成原任务失败，也近期保护已按11/17用户指令独立改用官方日志计数，见下节。

已经核验的执行可通过以下只读入口补充当前观察，不再走提交流程：

```bash
.venv/bin/python scripts/archive_price_logs.py \
  --authorization /absolute/path/to/authorization.json \
  --execution-id ORIGINAL_EXECUTION_ID \
  --output runtime/price-log-archives
```

原authorization文件同目录必须有对应intent文件。CLI强制关闭自身live_submit_enabled，
只调用lookup；不更改全局试点或自动开关。只接收live且verified的原执行，核对执行ID、
完整目标及上传/营销价后追加price_record和Audit。保留原状态、confirmed_at、effective_at、
执行更新时间和VariantGuard；不会重置72小时起点。

每次补档创建独立版本目录，保存price-record.json、official-queries.json、
original-verification.json、中文价格日志存档及sha256-manifest.json。数据库保存最新补充
观察，历史版本文件及审计保留；不回填旧批次的原始计算或伪称当时已经采到这些字段。

该接入当前覆盖后端和既有人工真实执行核验链路。通用HttpAdapter未提供四价来源时保持
原有核验行为，不伪造价格记录；前端专项展示和全店批量查询留待后续明确需求。

## 近三天调价保护（2026-11-17）

近72小时保护使用开放接口`getOperationLogCountBySellerSkuIdsAndTypeAndCompleteTime`，按子商品ID、type=2及北京时间完成时间查询滚动72小时。明确count>0不调价，明确count=0才通过；缺失、重复、异常或失败为未知，阻断对应目标。接口未确认只统计成功记录，故按存在调价日志判断，不限定操作者或涨跌，不伪造最近生效时间。本地档案保留人工保护和未完成提交锁，不再充当真实近期调价来源。

`capture_recent_price_changes`按唯一子商品ID每组最多100个查询，保存请求窗口、逐SKU状态/计数、响应哈希和来源编号。参数精度为秒，范围为查询时刻减72小时至查询时刻，不扩大到四个自然日。接口起止时刻包含性未单独验收，精确落在边界秒的记录以官方返回为准，不宣称已证明恰好72小时的排除行为。

ERP只读准备、人工真实适配器的输入准备和PricingService真实模式均读取这一来源。正式自动提价前再查当前目标，捕获批内缓存期间新增的外部调价日志。有记录标记“不调价”，查询未知标记“待处理”，保存对应判定及查询证据。独立有效SKU继续处理。经明确授权的人工修正沿用既有跳过自动72小时筛选的规则，未完成提交锁仍有效。

`OnlineState.recent_price_log`保存count、window_start、window_end、source_id，计数不能转成last_increase_at。真实模式移除本地生效时间的冷却判断；demo/file仍消费各自隔离的合成/文件生效历史，不访问真实账号。原历史批次不倒填新证据。
