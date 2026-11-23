# 亏损提价全链路进度与验收

## 2026-11-10

从 WSL 项目根目录运行。工作区已有采集、预览、执行和前端的未提交改动，本轮以当前工作树为基线。

| 阶段 | 已核对 | 待验收 |
| --- | --- | --- |
| 采集、归档与清洗 | 按指定编号将 7 个已保存批次各重放两次。检查 SQLite 外键及原值一致性，历史核对表仅参与比较。杨颖 1,068 条来源观察、572 个独立订单，覆盖历史 571 单，重叠订单的状态和修正一次毛利观察集合一致。桂露 213 条来源观察、111 个独立订单，覆盖历史 111 单，采集内部有 1 单跨批次存在多个财务版本，另有 4 单的历史与采集状态或修正一次毛利观察集合不同。 | 桂露三份订单财务原件的实际哈希与登记不符，原件验收未通过。需定位归档原件的历史版本，并将四笔差异串到可核对的预览。 |
| 字段与 SKU | 现有清洗按采集编号和文本订单号存订单及财务版本，类别仅保存在观察与问题的来源记录。同一批次中，同内容只存一个版本，item ID 去重排序后仍是父产品候选集合。跨批次通过订单号回查，保留各自时间与版本。 | 用与指定采集同范围同时间的商品明细、在线变体和稳定明细 ID 核实实际映射，补齐可核对的逐明细亏损证据。 |
| 只读预览 | 已有本地合成样例及规则测试。验收脚本可按真实历史编号产出缺项报告，接受绑定同一编号的补充证据。 | 历史分组各自采集完成不能证明同范围四类完整。还需完整候选与匹配时点的在线价、折扣、销量、保护与活动、历史、费用和汇率，才能逐条验收真实建议价。 |
| 隔离执行 | 现有测试涵盖未知结果只回查、有效价核验和运行模式隔离。 | 从已经验收的预览数据出发，串联人工复核、刷新、模拟接口及回查，分别验收防重和部分失败。 |
| 性能 | 验收脚本记录归档读取及清洗耗时、原件大小与清洗库大小。 | 对同一全量快照测量归档大小和按单号回查，确认瓶颈后再比较前后结果。 |

### 复现

将示例环境变量指向本机已有采集编号及尚未创建的隔离目录。指定编号的来源文件留在运行目录内，验收结果放在 Git 忽略的路径。

~~~bash
PYTHONPATH=backend .venv/bin/python scripts/verify_erp_replay.py \
  --source-runtime runtime --output "$REPLAY_DIR" "$CAPTURE_ID"
~~~

脚本核对原件字节数和 SHA256，再按采集时的业务列规则读取已完成的亏损名单和财务子批。它比较名单、候选订单号和财务行投影，写入独立的 `erp-clean.sqlite3`、`preview.json` 和 `summary.json`。可用 `--evidence` 指定补充证据。原件缺失、无法读取或与归档不一致时，脚本保留诊断输出并返回非零状态。命令在本地离线运行。

历史对账复现使用本机 `runtime/verify-erp-clean-20261110.py`。其数据、采集编号及历史对账文件位置由本机保管。运行前在隔离目录建立 `runtime/source-captures` 只读用途的链接，确保脚本写入隔离目录的 SQLite。该脚本的哈希差异输出须单独核对，脚本退出码仅表明其断言结果。

下一轮先核实原件差异和固定样例，再补齐指定批次的明细与在线证据。真实提交、生产部署和通知须另行授权。

### 本轮回放输出

| 输入 | 归档读取 | 清洗 | 清洗库 | 原件 | 预览 |
| --- | ---: | ---: | ---: | --- | --- |
| 杨颖一份亏损来源归档 | 0.002 s | 1.818 s | 286,720 B | 2 份均匹配 | 83 单缺明细证据 |
| 桂露两类亏损来源归档 | 0.003 s | 1.672 s | 360,448 B | 4 份中 2 份财务原件不匹配 | 111 单缺明细证据 |
| 运行目录中最大的已保存 JSON 归档（27,145,976 B） | 1.082 s | 2.719 s | 17,039,360 B | 14 份共 6,263,808 B，均匹配 | 5,449 单缺明细证据 |

以上是单次测量。最大归档重放产生 9,970 条观察、5,462 个批内财务版本和 13 个问题记录，三个报告均无价格建议。原件校验统计来源组 `files` 中的下载文件。生成的核对表另由历史对账脚本校验。运行输出见本机 `runtime/goal-replay-20261110/{yangying,guilu,largest}/summary.json`，第三份桂露原件的差异来自另一个历史归档。

正常 CLI 入口也已在隔离目录重放桂露的两类亏损来源，返回 111 个订单，并写入带采集编号的 `capture.cleaned` 开发日志。

在同一最大 JSON 归档上，取第一个已返回财务行的订单，100 次内存回查平均 2.47 ms，5 次包含归档读取的回查平均 282.79 ms。当前主要耗时在读取与解码，单次测量不足以证明优化幅度。以下命令以 `CAPTURE_ID` 指定相同归档。

~~~bash
PYTHONPATH=backend .venv/bin/python - "$CAPTURE_ID" <<'PY'
from pathlib import Path
import sys
from timeit import timeit
from app.services.erp_capture_store import find_order, load_capture

root = Path("runtime")
capture_id = sys.argv[1]
capture = load_capture(root, capture_id)
order = next(group["rows"][0]["orderid"] for group in capture["groups"] if group["rows"])
assert find_order(capture, order)["orderid"] == order
print("cached lookup ms", timeit(lambda: find_order(capture, order), number=100) * 10)
print("read and lookup ms", timeit(lambda: find_order(load_capture(root, capture_id), order), number=5) * 200)
PY
~~~

### 回归命令

~~~bash
.venv/bin/python -m pytest -q \
  tests/test_erp_bulk_capture.py tests/test_erp_capture_storage.py \
  tests/test_erp_clean_capture.py tests/test_erp_export_mapping.py \
  tests/test_erp_logging.py tests/test_erp_pricing_preview.py \
  tests/test_execution.py tests/test_execution_safety.py tests/test_coordination_execution.py
~~~

固定样例的两个案例、六条观察已按采集编号、来源与行号从原归档核回清洗库。另用合成采集和绑定同一编号的补充证据跑通新命令。输出 3 条规范明细、2 个判定、1 个建议，建议上传价为 20.80 MYR，另保留一条缺明细亏损的准备问题。
本轮相关后端回归 180 项通过，耗时 160.29 s。Starlette 测试客户端报告一条弃用警告。


## 2026-11-10 续验

- **真实历史归档与预览**。预览 JSON 的 `references.order_finance` 按订单号保留每条财务观察的来源、归档行号、店铺、状态、状态匹配标记和修正一次毛利原文。用桂露历史对账独立找出的 4 笔差异逐条核验，预览中的状态和金额观察集合与隔离重放的清洗库一致，且与历史对账值不同。每笔比对记录在本机 `runtime/goal-replay-20261110/guilu-finance-trace/`。复现命令为 `cd runtime/goal-replay-20261110/work && PYTHONPATH=../../../backend ../../../.venv/bin/python ../verify_finance_trace.py`。历史表仍只参与验收。
- **原件差异**。三份哈希不符的财务 XLSX 按各自实际导出子批的候选订单集合核对。现有解析器读取 29、82、1,000 笔，各子批无缺单、额外订单或业务列值差异。未在本机运行目录的同名同大小文件中找到与登记哈希相同的副本。原件完整性仍未通过，未改动原件与登记信息。复现命令为 `PYTHONPATH=backend .venv/bin/python runtime/goal-replay-20261110/audit_originals.py`。
- **真实导出映射**。将同一四类完整归档与已保存的两份真实导出按下述命令回放。5 单的 10 条 ERP 明细全部匹配到 9 个在线变体，其中 1 个涉及跨单。10 行的 SKU 修正毛利均空，5 单保留 `export_detail_identity_pending`，其余 5,444 单保留 `missing_order_details`，建议价为 0。输出位于本机 `runtime/goal-replay-20261110/mapping/`。
- **计算回归**。相同规范明细下，调整一个订单的重复来源归属并颠倒父商品提示的顺序，验证候选、选中明细、单件亏损、拦截和建议价一致。修改对账毛利观察值，验证价格判定完全一致。无前端变更。
- **当前缺口与下一步**。按原合同核实稳定明细 ID、同明细亏损及币种、对应时点的在线状态与费用和汇率。源端原件完整性需有权访问下载任务历史或保留原件的人员提供登记 SHA256 匹配的副本。后续继续在隔离环境串联复核、模拟提交和有效价回查，真实提价保持关闭。

~~~bash
PYTHONPATH=backend .venv/bin/python scripts/verify_erp_replay.py \
  --source-runtime runtime --output "$REPLAY_DIR" \
  --erp-details "$ERP_DETAILS" --sales-variants "$SALES_VARIANTS" "$CAPTURE_ID"
~~~

其中 `ERP_DETAILS` 和 `SALES_VARIANTS` 指向四类完整归档原件目录中的 `sku-erp-detail-export-probe.xlsx` 与 `sku-sales-readonly-probe.xlsx`。用本机对应采集编号绑定同一归档。可额外传入 `--evidence` 运行有证据的算价。

本轮 `.venv/bin/python -m pytest -q tests/test_erp_pricing_preview.py tests/test_erp_export_mapping.py tests/test_erp_clean_capture.py tests/test_erp_capture_storage.py` 共 60 项通过，耗时 15.14 s，含一条 Starlette 既有弃用提醒。命令行只提供其中一份 XLSX 时已验证拒绝执行，并且没有创建输出目录。


## 2026-11-10 隔离执行验收

新增 `tests/test_erp_replay_execution.py`，从现有合成四类归档和补充证据经管理员 API 生成只读预览，读取该报告保存的规范快照，用临时 SQLite、demo 适配器和固定业务时点演练。模拟刷新只更新在线观测时间，异常用例另修改明确指定的字段。文件对账报告仍只供查阅。

| 用例 | 验收证据 |
| --- | --- |
| 人工复核和授权 | ERP 预览选中明细与 demo 批次一致，目标实际价 18.72 MYR、含税上传价 20.80 MYR。读取商品当前控制版本后提出修正，批准前提交 0 次。重复批准复用同一个持久任务。 |
| 提交前刷新 | 人工批准后变更在线零售价。worker 刷新发现依据变化，将修正置为失效，外部提交、回查和执行记录均为 0。 |
| 未知结果与防重 | 通过模拟适配器复现请求送达后超时。持久执行为未知且保留目标锁。重建服务实例并重处理同一修正任务，沿原执行编号回查。开放模拟回执后核到 18.72 MYR，修正与执行状态转为已核验，释放锁。 |
| 部分失败 | 合成预览含两条可执行目标，批次在首次评估后统一刷新一次。一条由模拟接口明确拒绝并保留失败记录，另一条独立核到 33.00 MYR。 |

~~~bash
.venv/bin/python -m pytest -q tests/test_erp_replay_execution.py \
  tests/test_erp_pricing_preview.py tests/test_execution.py \
  tests/test_coordination_execution.py tests/test_sales_access.py
~~~

本轮只用合成快照验证执行。上轮真实归档仍只读，5 单 10 条真实映射缺明细亏损及完整在线算价证据。真实提交、部署和通知仍须另行授权。下一轮继续核对真实来源缺项，并以同一全量归档对比订单回查性能。
相关回归 59 项通过，含一条 Starlette 既有弃用提醒。运行日志保存在本机 runtime/goal-replay-20261110/execution-regression.log。


## 2026-11-10 订单回查性能

已保存采集的 API 单号回查复用同进程的解码结果，仍使用原有订单过滤。最多保留两个文件解析结果，足以覆盖压缩归档和清单。每次回查按文件设备、inode、大小、修改与变更时间确认缓存版本。替换、删除、压缩文件损坏以及调用方修改返回对象均有测试。整批读取、离线清洗、CLI 单次查询仍直接读取归档。未更改归档格式或接口返回。

同一进程分别对每个输入的 3 个订单执行 5 轮，测量本地服务函数，不含 HTTP、鉴权与网络耗时。新旧路径抽样返回一致。

| 输入 | 原路径中位数 | 新路径首查 | 新路径重复查中位数 |
| --- | ---: | ---: | ---: |
| 83 单的历史来源样本 | 1.837 ms | 2.095 ms | 0.081 ms |
| 5,449 单完整归档，原 JSON 27,145,976 B | 202.411 ms | 207.314 ms | 1.492 ms |
| 同一完整归档用当前写入器在隔离目录压缩，2,280,900 B | 200.274 ms | 208.924 ms | 1.235 ms |

收益来自同进程重复查询同一批次。冷查、换批次和进程重启仍需解码。解析结果占用进程内存，测量未包含并发和长期内存压力。

~~~bash
PYTHONPATH=backend .venv/bin/python scripts/benchmark_erp_lookup.py --runtime runtime "$CAPTURE_ID"
.venv/bin/python -m pytest -q tests/test_erp_capture_storage.py \
  tests/test_erp_clean_capture.py tests/test_erp_logging.py tests/test_erp_pricing_preview.py
~~~

相关回归 58 项通过，含一条 Starlette 既有弃用提醒。本轮只读既有业务归档，压缩对比副本在临时目录生成。未涉及前端。下一步继续核实指定真实批次的逐明细亏损、币种和同时点在线算价证据，原件哈希差异仍需登记副本。


## 2026-11-10 真实明细字段证据审计

新增 `scripts/audit_erp_detail_evidence.py`。读取已保存的 ERP 明细探针、ERP 导出投影、双接口摘要和真实只读预览，以订单号、原 SKU 和 Decimal 件数的多重集合核对。工具只输出计数。隔离副本中修改一条件数后，工具以非零状态拒绝。原件未改动。

| 字段 | 本机已保存证据 | 验收边界 |
| --- | --- | --- |
| 订单号、SKU、件数 | 5 单 10 行在探针、ERP 导出投影及预览完全对上，10 行均有在线目标。 | 这组证据可核对范围和件数。 |
| ERP 明细 ID | 三个多明细订单的导出「交易号」各自在单内重复，原探针 10 行的 `id` 均为空。另存的双接口摘要覆盖 1 行和 4 行的样本，记录了两端的 `transactionid`。 | 「交易号」不能作为明细主键。需 ERP 来源负责人确认 `transactionid` 的含义及稳定性，并给出真实同单同 SKU 多行样例。 |
| 亏损与币种 | 探针 10 行都有 `currency`，但 `adjustGrossProfit` 全空，ERP 导出的 10 行 SKU 修正毛利也全空。 | 币种字段有值无法证明它适用于该毛利列。需要含真实负金额的同明细记录和金额币种、单件或行合计的口径说明。 |
| 在线输入与建议价 | 已保存预览能核回 9 个在线变体，建议价 0 条。 | 需销售系统和财务负责人为选定目标提供同业务日的主 SKU 与仓配、含税在线价和营销价、保护活动与调价历史、费用和汇率证据。若历史时点不可补齐，使用同一业务日的新四类只读批次与配套证据。 |

~~~bash
.venv/bin/python scripts/audit_erp_detail_evidence.py \
  --probe-dir "$PROBE_DIR" --preview "$REPLAY_DIR/preview.json"
~~~

`PROBE_DIR` 指向包含 `sku-readonly-probe.json` 的本机已保存探针目录。`REPLAY_DIR` 使用同一四类归档的真实映射回放输出。本轮计数和反例日志保存在 `runtime/goal-replay-20261110/detail-evidence-summary.json` 和 `runtime/goal-replay-20261110/detail-evidence-negative.log`。无生产代码和前端改动。原件完整性仍需匹配登记哈希的副本。


## 2026-11-10 历史归档的明细证据

- 对运行目录中符合采集编号格式的归档使用正常加载器回扫。7 个归档带逐单数据，涉及 43 个不同订单、69 条明细端行观察、51 条财务端行观察，两端均无非空 `adjustGrossProfit`，也无已留存的 `transactionid`。3 个不符合正常采集编号格式的文件未计入。复现命令为 `PYTHONPATH=backend .venv/bin/python runtime/goal-replay-20261110/audit_archived_lines.py`，汇总在同目录的 `archived-lines-summary.json`。该结果限已保存投影，不能说明源系统从未返回 ID。
- 当前样本诊断保留返回的 `transactionid` 作为候选键；独立探针已观察到该字段，尚未核实唯一性和亏损归属。下一步仍需同明细非空亏损及同业务日在线证据。三份原件的字节完整性待匹配副本。



## 2026-11-10 当前工作树回归与整批证据核对

使用同一份真实四类完整归档重新生成独立的清洗库和缺项预览。14 份下载原件哈希匹配。隔离 SQLite 通过外键检查，5,449 单的 9,970 条财务观察在预览与清洗库之间按订单号、来源、行号、状态、修正一次毛利和店铺逐条一致。每单都有缺项原因，5 单为已有匹配但明细证据未齐，5,444 单缺订单明细。真实预览仍无建议价。回放目录为本机 `runtime/goal-replay-20261110/final-readonly-check`。

~~~bash
# 先按上文带两份导出的命令生成新的 REPLAY_DIR
.venv/bin/python scripts/verify_erp_full_trace.py "$REPLAY_DIR"
npm --prefix frontend run build
.venv/bin/python -m pytest -q
~~~

前端构建通过。构建和回归日志分别位于本机 `runtime/goal-replay-20261110/full-build.log` 和 `runtime/goal-replay-20261110/full-regression.log`。本轮未修改前端。本地隔离模拟执行与真实来源只读结果分开验收。

后续真实算价需来源负责人提供同一业务日、同一范围的证据。ERP 侧须确认稳定明细键并给出可归属到明细的负金额、币种和粒度。销售系统及财务侧须给出同目标的在线价、销量、保护与历史、费用及汇率。桂露三份原件仍需能匹配登记 SHA256 的历史副本。
全量后端回归 563 项通过，耗时 338.19 s，含一条 Starlette 既有弃用提醒。整批核对反例修改隔离副本中的一条毛利值后返回非零状态，失败输出见本机 runtime/goal-replay-20261110/full-trace-negative.log。


## 2026-11-10 真实证据阻塞复核

- 对上一轮整批回放后新增的源文件和补充证据重新盘点，本机运行目录没有新增匹配输入。现有批量采集合同只保证订单级财务范围。已配置的真实适配器合同文件不存在，真实提交配置为关闭。现有只读 ERP 汇率来源仍缺可用于回放的生效日和已确认的方向。
- 在运行目录、桌面项目资料及本机历史对账目录中扩大搜索桂露三份原件，按登记字节数筛选所有 XLSX，不要求同名，再以登记 SHA256 验证，找到匹配副本 0 份。业务列值一致仍单独记为字段核对结果。
- 该缺口已连续多轮出现。离线清洗、真实归档缺项追踪、SKU 抽样映射、隔离模拟和回归均已有验证。继续用现有输入重放无法产生真实建议价证据。
- 恢复真实验收需要同一业务日、同一四类范围的受控证据包。ERP 负责人确认稳定明细键及可绑定的负金额、币种、粒度，并提供同单同 SKU 多行样本。销售系统与财务负责人提供对应目标的含税在线价、营销价、保护活动、历史、费用版本、站点仓配合同、当日汇率及方向。持有原件的人员提供三份登记哈希匹配的历史副本。可先提交其中一套代表性真实算价样本以恢复逐条只读验证，其余覆盖仍逐项验收。


## 2026-11-10 下游功能模块续做

真实样本验收依赖来源证据。按用户澄清，下游功能开发与隔离验收继续推进。

- 算价使用已有 Decimal 引擎和费率、站点合同。合成 ERP 预览与执行批次核对同一明细，目标实际价 18.72 MYR、含税上传价 20.80 MYR。批次记录算价依据，执行记录保存提交请求与有效价回查证据。
- 通知数据库复用现有 `notices` 表和按批次、收件人去重的唯一键。批次汇总与待发通知在同一数据库事务提交，避免中途退出留下已完成批次却缺少待发记录。刷新已有通知只更新当前汇总，保留投递与确认记录。通知实际调度在事务之外。
- 隔离 SQLite 验证现有 Alembic 链升级到 `e63f8c2d05b1`。本轮事务改动无需新迁移。

~~~bash
.venv/bin/python -m pytest -q tests/test_notifications.py tests/test_erp_replay_execution.py
LR_DATABASE_URL=sqlite:///runtime/module-acceptance/notification-migration.sqlite3 \
  PYTHONPATH=backend .venv/bin/python -m alembic -c backend/alembic.ini upgrade head
~~~

运行迁移复现前先创建本机隔离目录 `runtime/module-acceptance`。真实提交、生产部署和对外通知仍按独立授权处理。
相关回归 36 项通过。故障注入验证批次状态和通知记录一起回滚，重试后仅一条待发通知。隔离 ERP 演练验证有效价回查后生成模拟通知，网页确认与投递状态独立保存。


## 2026-11-10 正常入口隔离验收

- 修复运行中的批次在第一条商品核验后提前写入批次完成状态和待发通知的问题。自动批次逐条持久化核验结果，结束时统一汇总并写入通知。独立后台回查仍会更新已经结束的批次。
- 验收从管理员登录和 ERP 预览 API 开始，通过工作台「运行批次」API 入持久任务，由 worker 领取。通过任务和批次详情 API 核对结果。模拟接口先受理并核验一项，下一项执行前批次仍在运行且未产生待发通知。第二项明确拒绝后，批次生成一条通知，状态汇总为一项已核验、一项待处理。
- 人工路径也通过同一运行入口生成待复核商品，再经修正、批准、worker、模拟查价、通知调度和网页确认。使用临时 SQLite、固定时点和模拟适配器。

~~~bash
.venv/bin/python -m pytest -q tests/test_erp_replay_execution.py tests/test_pricing.py tests/test_execution.py tests/test_worker.py
~~~
本轮相关回归 92 项通过，含一条 Starlette 既有弃用提醒。验证入口为应用 HTTP API 和 worker，未做浏览器手点。


## 2026-11-10 浏览器演示验收

本机回环地址启动独立 demo 应用与 worker。使用临时 SQLite 和独立演示账号，给虚拟店铺配置演示归属。浏览器依次登录、运行演示批次、进入通知收件箱、打开涉及商品列表并确认知悉。页面显示 8 条商品，4 条已核验、2 条待处理、2 条本轮不调。通知详情核对目标实际价 11 MYR、回读 11.00 MYR，以及单独记录的业务确认时间。

隔离数据库只读核对通过。批次已完成，4 条执行均已核验，唯一通知为模拟投递，外部任务号为空，管理员确认已持久化。外键检查通过。启动脚本与运行目录保存在本机 `runtime/module-acceptance`，启动命令为 `PYTHONPATH=backend .venv/bin/python runtime/module-acceptance/run_browser_demo.py`。此轮未修改业务代码。真实样本算价仍待同一业务日的明细金额与在线证据。


## 2026-11-10 全新演示库入口

开启销售协作时，全新隔离库点击「运行演示批次」原先只能算价，缺少归属导致可提价商品全部停在待处理。演示入口现在只为已知虚拟试点店铺补齐缺失归属，再经现有任务队列交给 worker。已有归属和人工控制继续生效。入口返回结构沿用现有任务编号。

用全新临时 SQLite 从 HTTP 登录、点击入口对应 API、worker 执行、回查批次，再核对模拟通知。修复前没有已核验项，修复后批次有已核验项，收件箱有一条模拟投递。相关回归 8 项通过。复现命令为 `.venv/bin/python -m pytest -q tests/test_api.py tests/test_erp_replay_execution.py`。真实样本验收仍需对应业务日的明细亏损和在线算价证据。


## 2026-11-10 缺汇率时的明细追踪

预览报告的明细引用新增在线目标、主 SKU 和亏损输入。即使汇率尚缺、引擎尚未开始算价，也能按采集编号、订单号和明细 ID 核对店铺、站点、在线子 SKU、主 SKU、原币金额、数量和金额粒度。金额与数量按十进制文本留存。

缺汇率用例验证明细引用保留完整且无价格判定。预览、导出映射和隔离执行回归 54 项通过。复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_pricing_preview.py tests/test_erp_export_mapping.py tests/test_erp_replay_execution.py`。真实样本仍需对应的明细金额及同业务日在线证据。


## 2026-11-10 导出证据冲突定位

预览的导出证据冲突记录新增两侧件数，或两侧在线目标及销售原件行引用。补充证据被拦截后，仍可按订单号和明细引用查看具体差异。合成双目标验收中，一条导出 1 件、补充证据填 99 件，该目标被挂起，另一独立在线子 SKU 仍能生成建议价。

复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_export_mapping.py tests/test_erp_pricing_preview.py`，52 项通过。真实建议价验收仍需稳定明细键、同明细亏损与同业务日在线证据。


## 2026-11-10 财务导出完成状态

修复预览把轮询中断后的部分财务行当成「导出完成但少返回订单」的问题。只有来源阶段明确为 `complete_with_missing`，并且候选名单与财务范围通过校验，才允许按已知目标隔离缺财务订单。未完成的来源继续拦截建议价。预览来源覆盖明细增加采集阶段和名单完整标记。

预览、导出映射与隔离执行回归 57 项通过。运行 `.venv/bin/python -m pytest -q tests/test_erp_pricing_preview.py tests/test_erp_export_mapping.py tests/test_erp_replay_execution.py` 可复现。完整真实归档再次回放，14 份原件通过校验，9,970 条财务观察与 5,449 单缺项一致。输出在本机 `runtime/goal-replay-20261110/coverage-gate`。真实建议价仍待稳定明细身份、亏损金额和同业务日在线证据。


## 2026-11-10 按单号定位财务原件

既有 CLI 与管理员 API 单号回查现在附带来源和该订单所在财务子批。子批有编号、结果状态和对应原件的归档路径、字节数及 SHA256。绑定依据为归档内该子批的请求订单清单与采集器生成的文件名。下载失败或尚未下载的子批可保留状态，原件列表为空。

合成双子批测试核对财务缺失订单仍能定位第一子批原件，第二子批订单只返回第二子批。真实完整归档按四组首条订单抽查，去重后 2 单命中四类来源，共定位 4 份原件，实测 SHA256 均与归档一致。采集、归档和清洗回归 43 项通过。复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_bulk_capture.py tests/test_erp_capture_storage.py tests/test_erp_clean_capture.py`，实际回查使用 `PYTHONPATH=backend .venv/bin/python -m app.capture_erp --capture-id "$CAPTURE_ID" --order "$ORDER_ID"`。真实算价与桂露三份历史原件仍需补充证据。


## 2026-11-10 清洗库来源缺项

离线重建现在区分来源未尝试、候选名单未取全和已知订单缺财务。前两项写到来源级问题记录，已知缺财务继续保留订单号和无财务版本的观察。旧归档未提供名单完整标记时保留原有记录依据，页号和错误原文仍可按采集编号从归档查询。

一份已保存真实不完整批次在临时目录重建，候选名单未取全记录为 1 条。未尝试来源与已知订单缺财务用合成归档验收。相关回归 44 项通过，复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_clean_capture.py tests/test_erp_bulk_capture.py tests/test_erp_capture_storage.py`。下一步真实价格验收仍待同明细亏损和同时点在线证据。


## 2026-11-10 回查与通知恢复

独立后台回查现将有效价证据、商品及执行状态、目标锁、批次汇总和待发通知放在同一数据库事务。批次内即时核验仍在整批结束时统一汇总。

在隔离 ERP 人工修正演练中，让外部模拟回执可查询，在通知写入后、提交前注入异常。修复前执行已经是已核验，后续无法按待核验状态重试。修复后异常回滚，执行保留未知状态和目标锁，再次查询原执行编号即可完成核验和通知更新，提交次数仍为 1。复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_replay_execution.py tests/test_execution.py tests/test_execution_safety.py`。真实算价证据缺口仍见前文。
隔离执行、回查和执行安全回归 54 项通过。后台批量回查在每个有新证据的结果提交时刷新该批汇总，本轮未测大量待核验结果集中返回时的吞吐。


## 2026-11-10 批次汇总与通知查询开销

通知汇总按批次分段读取商品和店铺归属，继续按商品级优先、店铺级其次的规则选择负责人。每个负责人仅核对一次账号状态。批次状态计数在数据库内分组。回查、批次和通知仍同事务提交，投递前权限检查继续执行。

用独立 SQLite 和 5,449 条合成商品按原方案与当前方案各运行三次。每次均核对批次和唯一通知的状态计数及商品数量。中位耗时从 5,080.5 ms 降到 180.0 ms，单次 SELECT 从 21,800 降到 15。测试使用单一虚拟店铺，测量范围是本地批次与通知汇总，不含有效价查询、外部投递或并发。批次汇总仍需读取该批商品，真实大批集中回查的整体吞吐另测。

复现命令为 `PYTHONPATH=backend .venv/bin/python runtime/module-acceptance/benchmark_batch_summary.py`。真实建议价验收仍待对应 ERP 明细亏损及同业务日在线证据。
通知、隔离 ERP 执行和销售协作回归 54 项通过。命令为 .venv/bin/python -m pytest -q tests/test_notifications.py tests/test_erp_replay_execution.py tests/test_coordination_execution.py。


## 2026-11-10 集中回查吞吐基线

新增 `scripts/benchmark_reconcile.py`。在临时 SQLite 中准备单一演示批次与持久未知执行，使用纯内存模拟有效价回执，经实际 `PricingService.reconcile` 完成查询、目标锁释放、批次汇总与通知更新。重复回查确认没有第二次查询，也禁止调用提交。每种规模校验全部执行已核验、目标锁清空、唯一通知汇总与批次一致。

运行 `PYTHONPATH=backend .venv/bin/python scripts/benchmark_reconcile.py 50 200 500`。50、200、500 条分别耗时 2.930、9.878、26.196 秒，对应每秒 17.1、20.2、19.1 条。测量包含本地逐条事务与通知刷新，外部网络时延和真实并发不在本基线内。这次没有修改运行逻辑。下一步可用同一脚本定位事务和重复汇总的开销。真实来源目录无新增采集，建议价验收仍待同明细亏损与在线证据。


## 2026-11-10 集中回查读取优化

通知汇总查询仅投影商品 ID、目标身份和状态。价格计算证据仍随商品和执行保存，汇总时无需逐条加载并解析。继续使用同一事务提交核验、批次及通知，投递前仍重新检查归属。

同一隔离脚本复测。50 条由 2.930 降至 2.884 秒，200 条由 9.878 降至 9.403 秒，500 条由 26.196 降至 21.791 秒。每个规模各一次测量，仅为本地串行模拟参考。全部核验、释放锁、无重传、通知与批次汇总一致的断言均通过。命令为 `PYTHONPATH=backend .venv/bin/python -u scripts/benchmark_reconcile.py 50 200 500`。真实建议价证据缺口见前文。
通知及隔离 ERP 执行回归 36 项通过，复现命令为 .venv/bin/python -m pytest -q tests/test_notifications.py tests/test_erp_replay_execution.py。


## 2026-11-10 人工修正模式隔离

人工修正的提案、批准与后台执行都核对原商品批次模式。模式不一致时，入口拒绝，后台任务在刷新在线价之前终止。文件对账批次的只读限制也在后台执行入口校验。缺少关联执行记录时，按该方案报错，避免把单条修正重试变成全量回查。

在临时库将演示修正的原批次标为另一模式。修复前可以进入模拟提交，修复后在收集在线数据前即被拦截，提交和执行记录均为零。同模式的正常复核和模拟回查沿用原路径。复现命令为 `.venv/bin/python -m pytest -q tests/test_coordination_execution.py tests/test_sales_access.py tests/test_erp_replay_execution.py`。真实算价仍待同明细亏损及同业务日在线证据。
销售权限、人工修正和隔离 ERP 执行回归 30 项通过，含一条 Starlette 既有弃用提醒。


## 2026-11-10 人工修正任务恢复链接

修正任务重试时验证已关联执行的批次确实属于该修正及当前运行模式。回查原执行后返回原批次编号，避免 worker 将原任务的批次链接置空。回归在同一方案重复执行时核对批次编号相同、提交仅一次。复现命令为 `.venv/bin/python -m pytest -q tests/test_coordination_execution.py tests/test_erp_replay_execution.py`。真实样本算价仍待同明细亏损和同业务日在线证据。
人工修正和隔离 ERP 回放回归 22 项通过，含一条 Starlette 既有弃用提醒。


## 2026-11-10 明确拒绝后的人工修正恢复

在隔离模拟中，提交返回明确拒绝并写入失败执行后，让 worker 在修正状态和批次汇总落库前中断。恢复时读取原执行，沿用已保存的拒绝原因，将修正状态和批次、通知汇总一并提交。再次处理沿用原批次，外部模拟提交次数仍为 1。

复现命令为 `.venv/bin/python -m pytest -q tests/test_coordination_execution.py tests/test_erp_replay_execution.py`。真实建议价仍待同明细亏损与同业务日在线证据。
人工修正与 ERP 隔离回放回归 23 项通过，含一条 Starlette 既有弃用提醒。


## 2026-11-10 财务原件接收时点

批量财务导出归档记录每个子批的原件接收完成时间 `downloaded_at`。预览用采集开始、轮询与已记录的下载时点一起检查业务日及评估时点。新字段表示接收观察时间，不能解释为 ERP 财务字段的实际更新时间。现存归档仍按其中实际记录的观察时点读取。

合成回归验证任务成功后晚于评估时点完成下载、跨北京时间业务日完成下载两种情况，均拒绝附算价证据的评估并保留只读观察时点。既有真实四类归档仍回放出 5,449 单、0 条建议价。采集、映射及预览回归 88 项通过，复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_bulk_capture.py tests/test_erp_pricing_preview.py tests/test_erp_export_mapping.py`。真实价格验收继续等待明细亏损和同业务日在线证据。


## 2026-11-10 在线价格时点一致性

只读预览与自动算价会挂起来源给出的价格变更时间晚于该价格观察时间的在线目标。人工修正的执行前复核也检查这一矛盾。用合成双目标验证，矛盾目标进入待处理，另一独立目标仍能生成建议价。校验只使用来源已给出的价格变更时间。

预览、算价和人工执行回归 130 项通过。复现命令为 `.venv/bin/python -m pytest -q tests/test_erp_pricing_preview.py tests/test_pricing.py tests/test_coordination_execution.py`。未发现新增真实采集，真实建议价验收仍待同明细亏损与同业务日在线证据。


## 2026-11-10 真实价格验收复核

重新用 `scripts/verify_erp_full_trace.py` 与 `scripts/audit_erp_detail_evidence.py` 对同一四类归档的隔离回放核验。9,970 条财务观察一致，5,449 单有缺项追踪。5 单的 10 条映射明细仍全部缺少 SKU 修正毛利。上次回放后源目录和运行目录均未发现新增归档或补充证据。下游本地和隔离执行验收已记录，真实建议价验收因同一来源证据缺口连续多轮无法继续。

恢复验收的第一份材料可限定为同一业务日、同一四类范围内的一笔代表订单及其在线 SKU。ERP 侧提供明细稳定键、可归属的负金额及币种和粒度。销售和财务侧提供该目标的在线价格、折扣、销量、保护活动、历史、费用合同和当日汇率。若历史时点无法补齐，可指定新业务日的同范围采集与配套证据。桂露原件完整性仍单独等待与登记 SHA256 匹配的三份原件。

## 2026-11-10 数据链路校验增强

- 四类完整归档离线核对 14 份原件的哈希和字节数，5,399 条亏损名单、9,970 条财务行逐条匹配归档投影。清洗库与预览的 9,970 条财务观察一致，5,449 单均有缺项追踪。历史反例有两份原件哈希不符，命令返回 1 并保留隔离报告。
- 采集遇到重复财务行时保留原件和观察记录，所在来源不报完成，后续子批可继续。重复业务列报来源错误，避免静默取后列。离线核对可区分原件缺失、字节不符和归档字段不符。
- 合成端到端回归通过管理员 API 采集四组模拟导出，随后经过清洗、明细和在线变体映射、补充证据、算价、文件对账入批及任务恢复。样例核对含税上传建议价 20.80 MYR，并验证文件批次没有创建执行记录。
- 预览逐个核对财务子批订单范围、结束状态和轮询时序。模拟复现子批失败、重叠、越界及倒序，均不能产生建议价。对已完成子批的重复订单，能定位到在线目标时仅挂起受影响目标。按单回查区分未选中、未发起、失败、缺行和重复行。
- 重放本机 10 份当前版本的归档，8 个来源通过财务覆盖核对，4 个销售员筛选来源仅供人工核对，2 个来源未完成。合成样例保持四类覆盖及 20.80 MYR 上传建议价。
- 子批请求与归档行、原件分别对账。隔离副本仅互换两批各一笔请求订单，原件哈希和来源并集仍一致，预览拒绝覆盖，离线验收精确报告两个子批范围不符。完整真实归档的 14 份原件仍通过，后端全量 579 项通过。
- 真实明细样本复核 5 单 10 行，匹配 9 个在线变体。10 行 SKU 修正毛利均为空。真实建议价验收需要同业务日的可归属明细亏损和在线算价证据。
