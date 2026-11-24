# ERP 采集对账与只读算价

## 手动测试：算好价格后停止，不提交

在 WSL 项目根目录运行 `PYTHONPATH=backend .venv/bin/python -m app.erp_workflow`。无需启动网页或 worker。未指定采集号时会使用本项目配置的账号读取真实来源，按部门依次处理。本入口在本进程内关闭价格提交，不改全局开关，也不暂停其他已运行的任务。算完即退出，不创建提交任务，留存的价格不会自动补交。
该命令一次调用完成四类候选抓取、原件归档、清洗入库、统一规则筛选和计算、待提价数据归档。
它只使用现有 Python 适配器和规则引擎，没有大模型调用，也不上传价格。

终端结束时会打印算价通过的条数、总报告以及各批待提交清单的路径。四部门运行的总报告 `result.json` 按部门列出状态；完整空名单、限流或失败的部门可能只有状态，没有清单。生成算价报告的批次在各自的 `runtime_dir/erp-workflows/<运行号>/` 下保存：

- `pending-prices.json`：仅包含算价通过的目标及其原始明细、公式和价格证据。
- `unresolved.json`：保存准备缺项、全局问题和全部未就绪判定（包括正常的本轮不调）。
- `result.json`：绑定采集号、报告号及报告 SHA256，记录统计和运行结论。

退出码 `0` 表示本次数据评估无未解决缺项；`2` 表示已归档，但仍有缺项或待处理；`1` 表示运行失败。
`completed` 只说明这次只读评估完成，不能证明真实数据正确性或价格已生效。
合成数据用 `synthetic=true` 标识。`blocked` 表示没有可提价结果且仍缺条件，不能将空待提价文件解释为成功。

只读实时运行按订单隔离原生明细未就绪、订单状态冲突或缺少在线子 SKU 映射：这些订单保留具体原因，但不再连带排除同店或同 SKU 的其他正常订单。若某一类 ERP 候选名单本身未取全，报告会指明名单名称，受影响部门不能将其当作零订单继续算价。以下显式文件回放仍要求补充证据与选定采集一致。

显式回放使用 `--capture-id <采集号>`，可附 `--evidence <证据.json>`，以及成对的
`--erp-details <明细.xlsx> --sales-variants <在线.xlsx>`。证据仍须绑定该采集编号，命令不自动改写绑定关系。
真实抓取接入四候选财务导出，并在抓取后自动查询公司当前汇率。按用户2026-11-11确认，查询的北京时间日期为业务使用日，原接口生效日期仍保留为空。
汇率观察独立保存在 `runtime_dir/source-fx/`，并随预览引用保留原配置、日期策略和展示倒数；计算直接使用原报价的精确比值，读取失败不回退旧配置。
独立明细亏损和完整在线状态仍待自主接入。实时命令能自主保存缺项报告，但在这些来源接入之前不能宣称真实算价闭环通过。

管理员在工作台进入 **运行配置 → ERP 采集对账与只读算价**。填写命令行或管理员采集返回的 `capture_id`，点 **生成只读报告**。可同时选择 **ERP 商品明细 XLSX** 和 **销售在线变体 XLSX**，核对采集订单到在线变体的匹配。取得稳定明细 ID、亏损、在线状态和汇率等独立证据后，附上补充 JSON 再运行。每次生成独立报告，可凭采集和报告编号回读或下载。

报告保存到当前运行目录的 `erp-pricing-previews/`。采集原始证据留在 `source-captures/`。预览 API 如下，均要求管理员登录。

| 入口 | 输入 |
| --- | --- |
| `POST /api/sources/erp/captures/{capture_id}/pricing-previews` | 补充证据 JSON 对象或 `null` |
| `POST /api/sources/erp/captures/{capture_id}/pricing-previews/from-exports` | multipart 文件 `erp_details`、`sales_variants` 和可选 `evidence_json` |
| `GET /api/sources/erp/captures/{capture_id}/pricing-previews/{preview_id}` | 采集编号及报告编号 |
| `POST /api/sources/erp/captures/{capture_id}/pricing-previews/{preview_id}/reconciliation` | 文件模式入队，返回 `job_id` |

原单可通过同一采集编号和 `orderid` 在采集接口回查。回查结果区分重复财务行、导出缺行、子批失败、未发起和试抓未选中。

财务覆盖按子批核对编号、请求名单、最终轮询状态和批次状态。按子批记录的行数切分归档财务行，核对订单归属及缺行、重复行和越界记录。时点按采集、子批轮询、原件接收的顺序核对。完成导出后的缺行或重复若能通过完整明细证据定位到在线目标，只挂起受影响的目标。无法核实子批覆盖或定位目标时，相应来源视为不完整。

## 两份原始导出

- ERP 商品明细使用「订单编号」「平台」「店铺」「商品SKU列表」「订单数量」「SKU修正毛利」。选中采集的财务表还须提供订单级「item ID」父产品候选集合。
- 销售在线变体使用「店铺名称」「站点」「产品ID」「商品ID」「本地子sku」「平台子sku」。按同店、候选父产品与本地子 SKU 找唯一在线变体。保存商品 ID 和平台子 SKU，保留平台子 SKU 中实际存在的 `@`、`#` 等字符。歧义保留证据引用与原因。
- 导出使用单工作表的 XLSX，必需字段采用原始值。上传正文限 5 MiB。每个工作表的实际坐标限 5 万行、128 列，每份 XLSX 展开限 128 MiB。原始文件的 SHA256 和物理行号组成引用。销售重复变体保留首个匹配引用，歧义保留能证明冲突的匹配。
- 第一次可只上传两份导出，下载报告取得各条 `detail_ref`。附补充证据时，每条已确认明细填写对应 `detail_ref`，`detail_id` 仍由独立来源确认。系统逐订单核对物理行覆盖，再比对在线变体和数量。不一致的记录留在报告中。映射阶段只观察 SKU 修正毛利是否有值，具体算价金额取自独立亏损证据。

## 采集和证据怎么衔接

1. 采集输入采用 2026-11-09 四类批量的 `erp-web-bulk-four-20261109` 格式。订单号以各组的 `candidate_order_ids` 为准，`rows[].fields` 关联订单财务。财务的 `item ID` 与 `商品SKU列表` 用于回查。具体在线商品 ID、SKU 明细行及其亏损金额取自独立证据。
2. 同一订单在多个来源出现时，报告保留各来源。同一条明细按订单号和稳定明细 ID 识别。样例中同订单的不同明细各自保留。现有引擎按具体在线子 SKU 取最小有效单件亏损。
3. 本轮只读回放的时点来自补充文件。评估时间须在采集开始和最后一次导出观察之后，且处于同一北京时间业务日。样例文件中的时间为历史回放时点。四类名单与财务对账、可定位的明细覆盖、规则判定分别列示。
4. 有完整名单且能定位目标时，缺财务行、缺金额、订单状态冲突会挂起相应目标。同一目标的其他订单也会显示受影响。财务表已返回但同一订单的店铺字段全空或互相矛盾时，无法核实补充映射，按未定位缺口处理。来源范围、完整明细或映射无法确认时，保留相应来源的未定位缺口，避免按不完整候选选出偏大的亏损。缺单个在线快照、站点合同或费用模型时，由引擎给出该目标的原因。
5. 报告保留采集号、订单号、明细引用、映射引用、汇率和合同版本，以及可计算项的规则过程。回放结果与生产调价批次分开。该入口只读取文件、运行规则和保存对账报告。

## 从报告生成文件对账批次

在独立文件对账环境（LR_ADAPTER_MODE=file），管理员可打开有规范快照的报告并点 **生成文件对账批次**。后台从该报告归档快照及原算价判定，保留原业务日和回放时点。等待任务完成后点 **查看对账批次**。以后重新打开报告，入口显示 **打开文件对账批次**。批次页保留采集号和报告号，并可直接打开原报告。

同一报告只生成一个批次。任务记录报告文件的 SHA256，worker 校验后将批次和商品判定一次写入。报告临时缺失时，恢复原文件后可再次点入批。若重新生成了证据或改动了报告内容，应使用新报告编号。已入批报告的原文件需与文件模式数据库一并备份。

只有可定位的规范明细才进入商品批次，缺失或歧义留在准备报告。只含原始导出映射或没有汇率的报告仍可回读。对账批次由管理员在 **运行批次** 查看，和当前商品队列分开。入批是本地归档操作，保留的价格为原算价建议。

## 补充文件 `erp-pricing-evidence-v2`

完整样例见 [erp-pricing-evidence.sample.json](../tests/fixtures/erp-pricing-evidence.sample.json)。程序内部字段与公司接口字段分别维护。

在线快照只提供当前价格、保护与历史和完整在线利润证据。文件补充合同采用 v2，实时在线准备采用 `erp-live-online-preparation-v2` / `erp-live-online-v2`；旧补充文件和在线准备须按当前合同重新生成，不自动升级或回写历史档案。ERP 原始财务下载与「修正一次毛利」列保持原样。

| 字段 | 用途 |
| --- | --- |
| `schema_version`、`capture_id`、`synthetic` | 固定填 `erp-pricing-evidence-v2`，绑定指定采集编号。样例标记必须与采集一致，真实采集为 `false`。 |
| `evaluated_at`、`source_versions` | 带时区的 ISO 8601 回放时点。记录 `details`、`mapping`、`online` 的证据版本。 |
| `orders[]` | 用文本 `order_id` 关联采集订单。全部明细可核实后填写 `details_complete: true`。来源未取全用 `false`。 |
| `orders[].lines[]` | 填写该订单内稳定的 `detail_id` 和 `detail_ref`。一个真实明细对应一条记录。缺少可靠在线映射时 `mapping` 填 `null`。 |
| `mapping` | 填写 `erp_shop`（ERP 财务表的店铺原文）、销售系统的 `key`、`main_sku` 和可回查的 `ref`。键含平台 `TikTok`、店铺 ID、站点、产品 ID、具体商品 ID、子 SKU 和仓配。 |
| `loss` | 填 `quantity`、带原始负号的 `amount`、原币 `currency`、`basis`、`allocation_reliable`、`fee_scope`、`abnormal`、`ref`。缺任何关键归属证据时填 `null`。 |
| `online[]` | 每条为 `ref` 与规范 `state`。保留在线观测时点、零售价、营销价、保护/活动、调价历史和完整在线利润模型。确认无折扣才填 `marketing_price: null`。在线字段缺失时，该目标的完整快照先留空。 |
| `fx` | 填业务 `date`、每 CNY 兑换的各币种单位 `rates`、`version` 和 `ref`。CNY 为 `"1"`。未取得日期或汇率方向证据时填 `null`。 |
| `contracts[]` | 每站一条 `ref` 与 `contract`，写清站点、仓配、步长、舍入规则、含税上传列和版本。回放依据为提供的证据与配置，真实适用范围仍须业务核验。 |

金额、数量、费率、步长和汇率使用普通十进制字符串。ID 也使用文本，保留前导零。亏损金额粒度为 `unit`（单件）、`line`（明细总额）或 `order`（可可靠归到单个同 SKU 明细的整单总额）。多明细订单应填可核实的逐明细亏损。证据引用填写能在受控位置回查的编号，勿填密码、令牌或下载地址。结构错误会拒绝该次请求，并在网页显示字段位置。证据确实缺失时按上表填空值，仍能生成缺项报告。

## 本地样例

采集夹具沿用今天的四组批量结构和财务字段。订单、店铺、商品、费率和价格均为合成数据。样例覆盖同单跨来源、同 SKU 跨单、同单多个 SKU、销量保护和缺少明细亏损。

在项目根目录运行下列命令，直接读取样例并打印摘要和价格建议。

~~~bash
PYTHONPATH=backend .venv/bin/python - <<'PY'
import json
from pathlib import Path
from app.services.erp_pricing import preview_pricing

fixtures = Path("tests/fixtures")
capture = json.loads((fixtures / "erp-pricing-capture.sample.json").read_text(encoding="utf-8"))
evidence = json.loads((fixtures / "erp-pricing-evidence.sample.json").read_text(encoding="utf-8"))
report = preview_pricing(capture, evidence)
print(report["summary"])
for result in report["decisions"]:
    print(result["key"]["sku"], result["reason"], result["target_actual"], result["upload_price"])
PY
~~~

MY 站样例中，A 选中订单 `000001` 的明细 `01`，单件亏损为 1.20 CNY。按样例汇率折为 0.72 MYR，从当前实际价 18.00 得到目标实际价 18.72 MYR，含税上传建议为 20.80 MYR。C 保留缺少明细亏损的原因。

桌面程序独立保存的 `result.json` 可由本机操作者显式登记。把命令中的文件路径替换为本次的完整路径，在 WSL 仓库根目录运行，打印的编号可用于工作台回读。

~~~bash
PYTHONPATH=backend .venv/bin/python - /path/to/result.json <<'PY'
import json
from pathlib import Path
import sys
from app.adapters.erp_pricing import BulkCapture
from app.config import get_settings
from app.services.erp_capture_store import save_capture

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
BulkCapture.model_validate(dict(data, capture_id="desktop-import"))
print(save_capture(get_settings().runtime_dir, data)["capture_id"])
PY
~~~

桌面试抓和单组采集可用来核对名单和缺项。四类完整范围和商品、费用、时点证据仍由各阶段分别验收。


## 财务观察回查

下载报告后，在 `references.order_finance[订单号]` 中查该采集批次返回的财务行。报告顶层的 `capture_id` 与观察的 `source`、`row_number` 共同定位归档中该来源的 `rows`，行号从 1 起算。跨批次对比时分别保留报告时点、来源与行号。缺财务的订单继续通过问题列表定位。

观察保留订单状态、店铺和修正一次毛利的原始十进制文本或空值。修正一次毛利属于订单粒度，随财务导出观察保存，不能当作逐明细亏损或单件金额。币种未在此观察中推断，算价仍使用绑定明细且具备独立币种与粒度证据的金额。


### 自主销售观测归档（2026-11-11）

实时入口在ERP原件及清洗保存后，按本轮财务 `item ID` 候选去重读取销售系统：父产品完整分页、逐店铺/父产品展开当前变体，以及公司费用汇率。2026-11-13起停止抓取动态费率，改用用户指定的本地固定BD表。来源响应逐份按原字节保存，记录请求范围、时间和SHA256；金额直接按Decimal解析。认证响应和令牌不归档。

归档目录为 `runtime/sales-source-captures/<source_id>/`，`result.json` 是结构化观测；预览 `references.sales_source` 保存原件清单引用及摘要哈希，绑定同一ERP采集号及synthetic/sample_only。抽样来源不能作为全量来源挂接。离线回放不会暗中补抓销售数据。

单批超时、5xx、缺身份或格式异常留下范围问题，独立范围继续读取；认证/权限失败停止该来源，不能用重登或旧文件替代。范围越界响应只留原件，不提升为有效观测。父产品没有匹配时保留 `missing_product_ids`；`acquisition_complete` 仅表示已计划请求及分页均成功，不表示候选全部在线、费用合同已审核或可以定价。

这些记录始终 `pricing_ready=false`。本地执行历史、当前费用与库存另外绑定本次采集，经规范准备后进入引擎。`sales_status=saved` 只表示观测采集完成，不代表六步业务闭环成功。


当前变体来源使用已核对前端展开合同 `POST /Api/api-tiktok-sales/online/findProductVariants/{shopName}/{productId}`，店铺及产品分别URL编码，请求体重复绑定同一店铺/产品。每个父产品独立校验范围并隔离失败。批量调价投影接口会将海外库存和当前海外仓利润率留空，已从生产采集路径移除，不作为后备来源。新增真实抽样中8条变体都取得这两类字段；这仍不等于费用模型、销量窗口和历史保护已满足定价合同。


固定费用来源归档在 `fixed-fee-sources/`；报告引用 `fixed_fees`。旧公司历史/生效观察路径和ERP销量窗口导出已退出本流程，旧历史文件不回填新来源字段。迁移及缺项见 [11/13实现说明](user-policy-update-2026-11-13.md)。


## 部门候选的销售批量导出（2026-11-18）

ERP 订单明细及财务来源核对后，按订单号归并重复来源，只提交修正一次毛利确认为负的候选对应的去重父产品 ID。每部门串行按每批最多 350 个产品 ID 向销售系统提交在线子 SKU 导出。文件按店铺、产品 ID、本地子 SKU 和站点匹配，使用 ERP 已确认的店铺 ID；批次文件保留任务编号和 SHA256。实测提交无任务 ID 时只接受本次提交后唯一新增的任务，且文件集团与父产品集合必须匹配请求。

批量文件带价格、营销价、折扣、VAT、尾类目、直发及海外仓公式利润率和 7 天销量。拿到商品 ID 后按子商品批查官方 72 小时调价保护。仅通过者补 ERP 子 SKU 按仓销量参考和必要平台现态。销售导出销量、公式利润率随 SKU 保留为来源证据。现价须与平台快照一致，泰国直发导出 VAT 和尾类目须与当前商品核对；现行费用模型仍负责按新价格复算利润。可售、活动、仓配和未能由文件完整证明的费用明细由现态接口核对。
