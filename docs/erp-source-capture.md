# ERP只读数据采集

## 按部门手动测采集耗时（独立脚本）

`scripts/benchmark_erp_department_capture.py` 参考四类导出工具，直接下载指定部门的候选名单与订单财务原件。只用于查看抓取结果和耗时，不生成定价快照、不写生产数据库、不上传价格、不发送钉钉消息。

现有四个部门的精确筛选名称为：`Tiktok一部`、`Tiktok五部`、`Tiktok十二部`、`Tiktok二十二部`。通过必填且可重复的 `--department` 指定，多个部门串行采集；名称不设白名单。默认每个部门抓四类全部订单，`--max-orders N` 只限制每类订单明细导出的数量，候选名单仍完整采集。

账号读取**本项目根目录** `.env` 的 `LR_ERP_USERNAME` / `LR_ERP_PASSWORD`，同名环境变量优先。不读取上级目录配置或压缩包的账号。平台、平台 ID、集团和请求间隔分别读取 `LR_ERP_CAPTURE_PLATFORM`、`LR_ERP_CAPTURE_PLATFORM_ID`、`LR_ERP_CAPTURE_BLOC`、`LR_ERP_CAPTURE_REQUEST_INTERVAL_SECONDS`，未配置时沿用项目默认值。无需开启真实提交、数据库或钉钉配置。

在 WSL 的项目根目录先小量试抓：

```bash
.venv/bin/python scripts/benchmark_erp_department_capture.py --department Tiktok一部 --max-orders 20
```

该部门四类全量（不加 `--max-orders`）：

```bash
.venv/bin/python scripts/benchmark_erp_department_capture.py --department Tiktok一部
```

四个部门串行全量：

```bash
.venv/bin/python scripts/benchmark_erp_department_capture.py \
  --department Tiktok一部 --department Tiktok五部 \
  --department Tiktok十二部 --department Tiktok二十二部
```

在 Windows 项目副本根目录，用该副本的虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_erp_department_capture.py --department Tiktok一部 --max-orders 20
```

其他参数：`--source loss_allocatable` 可只抓一类并可重复指定；`--batch-size` 默认 1000；`--request-interval` 覆盖配置中的请求最小间隔（通常为 16 秒）；`--output` 指定输出根目录。禁止发货固定每页 100 条。任务查询最多 225 次，按请求间隔执行，任务创建仅提交一次，失败或未知不自动重提。该脚本不借用生产进程的请求节流状态；手动运行时请留意是否已有生产采集在使用同一账号。

默认每轮输出到 `output/erp-department-<时间与随机编号>/`：

- `progress.log`：同时打印到终端的阶段、分页和批次进度，不输出凭据、令牌或下载地址。
- 根目录 `result.json`：整轮状态、起止时间、实际耗时、请求数、限流次数、各阶段耗时及跨部门/来源按文本订单号去重后的计数。
- 各部门/来源目录：`候选名单.json`、亏损来源的 `亏损池.xlsx`、`订单明细-001.xlsx` 等原始文件，以及该组的 `result.json` 检查点。包含本次任务名称，便于人工核对未知或等待失败的任务，不保存带权限下载地址。
- 每组报告记录候选数、选取并尝试导出的订单数、下载行数、下载唯一订单数、缺失及额外返回的订单号、部门不符/未知行数、文件大小、失败页和批次。失败或未返回数据不会补零。空名单是正常的零订单；后页失败保留前页名单，单批失败继续其他批次和来源。动态总数变化不作为失败。

`timing.http_seconds` 为 HTTP 请求耗时，`timing.throttle_seconds` 为请求间隔等待耗时。`timing.stages` 分别记录 `login`（登录）、`candidate_collection`（名单采集）、`export_submit`（导出提交）、`task_wait`（任务查询等待）、`download`（下载落盘）、`parse`（Excel 解析）。阶段耗时包含该阶段发生的请求及等待；名单采集也包含亏损池解析，因此这些字段**有重叠，不能相加当作整轮耗时**，整轮以 `elapsed_seconds` 为准。各组另有自己的计时差值。

完整采到所选数据且未发现缺行、部门归属等问题时退出码为 0，否则为 1 并保留结果。小量运行的 `complete` 仅表示选中样本完成，根报告 `sample_only=true`；这些下载结果不代表已具备提价资格。按 Ctrl+C 中断时保留已有原件和来源检查点，汇总会标记中断；检查点中 `running` 的批次可能已提交，重新启动脚本会创建新一轮，不会自动恢复旧任务。

原始 Excel、订单名单及报告均含真实业务数据，应仅本地留存，不提交 Git 或转发到公共位置。实际耗时需要手动运行观察；模拟单元测试不代表真实 ERP 采集验收。

## 2026-11-25 部分成功处理

实时 ERP 改为按本轮实际采到的有效订单继续处理。后续分页失败时，已验证前缀仍导出财务并进入算价；财务子批失败时，其他成功且逐批对齐的子批继续。同页身份、平台、状态、禁止发货标记或指定人员不符的行记录在 `excluded_rows`，仅排除该行；动态重复观察去重，总数与页长变化不作整批拦截。财务串单、重复或冲突仍保留定位证据，不以未知金额补算。

`capture_complete` 和 `list_complete` 继续表达真实完整性，不因部分可算而改成成功。实时准备通过 `candidate_scope=observed_erp_orders` 区分实际样本范围，未取全来源及排除行在 `collection_warnings` 留因；即使一个独立来源完全失败，其他可靠来源仍可处理，不将失败来源表示为零。全部无可靠样本时不生成可执行价格。通用 HTTP 与规范文件合同保持原义，未知导出不重提，价格上传授权与核验不变。当前采集策略为 `observed-orders-20261125`，下文 11/24 的策略描述仅作为其数量检查规则依据。

## 当前采集入口

默认批量入口读取 **亏损订单／禁止发货 × 可配货／待处理** 四类 TikTok 候选，集团取当前 ERP 配置。

### 按部门运行只读提价流程

- `PYTHONPATH=backend .venv/bin/python -m app.erp_workflow` 依次运行 Tiktok 一部、五部、十二部、二十二部。每个部门独立采集、归档和算价，上一部门处理结束后才开始下一部门。失败和未完成保留结果并继续下一部门。指定 `--capture-id` 可离线回放对应采集。
- 亏损池导出用 `department`，禁止发货列表用 `departments`。2026-11-13 的只读归档记录了这些筛选条件。禁止发货列表的部门字段返回为空，按订单号导出后用财务表的「部门」列逐行复核。部门不符或缺失的批次保留原件和错误，财务行不进入算价。
- 财务导出按来源分批。核对店铺归属后生成部门的 `seller-candidates.json`，按销售员汇总四类候选，按「店铺 ID＋订单号」去重并保留全部来源标签。店铺无法核实、销售员缺失或冲突的候选列入 `unresolved`。财务原件及算价输入继续保留各自的来源记录。
- 生成建议价前，以销售系统当前在线商品的部门复核归属。跨部门或归属缺失的商品保留为待处理。
- 整轮汇总和各部门的采集编号、状态、算价档案位置记录在运行目录下 `erp-workflows/<运行编号>/result.json`。ERP 返回账号限流码 500003 时，只记录当前页或子批失败；已取订单继续取证算价，后续独立批次、来源和部门照常采集。超时或结果未知的导出任务不会自动重提。入口只生成只读结果。

### 固定人员任务已删除（2026-11-26）

已删除固定七人姓名／部门表、`--roster-date` 参数、名单日强制定时、专用价格提交与零库存续跑入口，以及 `LR_ERP_ROSTER_*` 配置。部门只读流程按实际采集结果处理，不再按七人筛选；普通定时任务仅由原有 `schedule_enabled` 开关决定，北京时间 05:00 起运行。历史名单记录保留查询和下载，已提交或结果未知的商品继续核验、不重传。销售员通知收件人改由本项目钉钉人员目录按姓名查询，不再维护七人 User ID 配置。

### 1. 集团范围的独立批量下载

- CLI `PYTHONPATH=backend .venv/bin/python -m app.capture_erp --progress` 和管理员 `POST /api/sources/erp/captures` 默认全量。可用 `source` 显式选择单类；CLI、独立桌面脚本使用 `--source`。
- 亏损两类用 `POST /Api/deficit/dowDeficitOrder` 下载原始名单，分别指定订单状态 `status=4/3`，均指定财务复核 `financecheck="3"`，排除自动放行等其他复核状态。复核待处理的 Excel 值可能为空白。
- 禁止发货两类用 `POST /Api/order/list`，按平台 ID、集团、`jzfh="1"` 和 `status=4/3` 分页，每页请求 100 条，继续翻页至结束。2026-11-24 对一、十二、二十二部授权只读核验：首页成功响应均为 HTTP 200、整数 `code=0`、`data` 对象、整数 `total` 和 `records` 数组。两个部门在 limit=200 读超时，改为 20 后均返回非空页。三份首页都不是零；未观察到 `null` 表示零的合同。整数 `total=0` 和空数组同现表示该次响应明确为零；动态名单的空页也按本轮无更多返回结束，不代表冻结范围全量为零。超时、结构缺失及业务失败不作零单。四类名单（含亏损 Excel）、任务查询及已绑定文件下载遇到读超时或 HTTP 5xx 总共最多尝试三次，每次仍节流；业务失败、认证和创建异步导出任务不重试。
- 2026-11-24 用户取消禁止发货动态名单的总数、页长一致性检查，不因数量变化标异常或阻断算价。遇到短页或页偏移达到当前响应总数即结束，不回头补采；组级 `total` 保存实际候选数，各页 `total` 保存源端观察值，策略版本为 `observed-pages-20261124`。仍逐页核对订单号唯一性、平台、状态、禁止发货标记及指定销售员；跨页重复或某行不符则整页不用。部门仍由已绑定的财务导出逐行复核。各页响应的结构摘录仅在当前部门本地原件目录 `*-page-*-excerpt.json`，只含 HTTP 状态、数字业务码、固定结构标签与计数，不含响应文案、原行、凭据、URL，不进入技术日志。
- 四类名单都保留文本订单号。按来源分别用 `asyncOrderListDownload` 的 `orderIdList` 分批提交，默认每批最多 1000，按返回任务名和 ID 在任务列表绑定，最多轮询 225 次。每批记录请求、实际、缺失、超范围和重复行，状态变动单列记录。失败批不自动重提，来源和后续批次独立处理。
- 财务表按订单核对。重复财务行连同原件保留，所在来源标记财务冲突，采集与试抓均不报完成。重复业务列无法确定投影时，保留原件并报告来源错误。
- `candidate_order_ids` 是本轮实际采到的有效候选名单，`list_complete` 表示本轮名单读取正常结束，不代表动态源端冻结快照；`batches[].scope_check` 记录财务对账。两类亏损另留名单原始 XLSX 和财务核对表。文件名前缀是来源名。
- `--max-orders N` 仅限制**每类**财务导出数量，桌面默认每类 20 笔，可用 `--all` 全量。按销售员采集默认覆盖四类来源，指定 `--source` 可单独核对其中一类。
- `capture_complete` 表示本次所选来源全量名单与财务导出核对完成；试抓看 `selection_complete`。本入口保留订单级财务原件与状态证据，不重算毛利。定价数据合同另见第 4 节。

### 2. 管理员小样本诊断

- 「运行配置 → ERP 数据获取验证」读取四组各第一页，并对样本订单去重读取商品明细。诊断报告可从网页下载；不查询逐单财务、不生成主采集归档或定价快照。
- 管理员可调用 `GET /api/sources/erp/validation?limit=2` 获取报告，或用 `GET /api/sources/erp/sample` 查看白名单样本。每组最多 10 条。
- 原 CLI 分页与逐单模式已下线。需要全量订单财务时使用批量入口，需要诊断报告时使用网页诊断。批量 API 只接受可选的 `source` 查询参数；旧分页参数返回 422，不会触发全量读取。已保存的采集归档仍可按编号查询。

### 3. 公司汇率：独立只读来源

- 实现：`backend/app/adapters/erp_rates.py::query_erp_rates`；入口为`GET /api/sources/erp/rates`。
- 使用公司`/open`服务的登录及`/system/innerOpen/rate/list`，与网页采集分别认证。
- 来源没有已确认的汇率生效日期，方向仍按实测推断；倒数仅供展示，未注入自动调价快照。查询成功不等于满足当日汇率合同。

### 4. 定价任务来源：与采集文件分开

- `backend/app/main.py::build_adapter`按配置选择`DemoAdapter`、`FileAdapter`或`HttpAdapter`；定价服务调用其`collect`得到规范`Snapshot`。
- file读取规范快照，不是直接读取ERP原始采集JSON；live的`backend/app/adapters/http.py::HttpAdapter.collect`按独立合同读取四候选、在线状态、汇率并转换字段，仍拒绝异步export合同。
- 已保存的四类 ERP 批量采集可以显式生成只读对账报告。按版本化补充文件提供明细归属、在线商品及费用证据后，沿用现有引擎算价。网页入口、证据格式及样例见[ERP 采集对账与只读算价](erp-pricing-preview.md)。实时定价任务仍使用配置的取数适配器。

### 5. 文件与核实边界

- `backend/app/services/erp_capture_store.py`负责保存、回读、按订单查询；不写定价数据库或执行意图。
- 批量原件位于有效 `runtime_dir/source-captures/erp-bulk-*/`，含亏损池与分批财务 XLSX。采集中以 `result.json` 记录检查点。项目 CLI/API 保存时发布 `result.json.gz` 和轻量 `erp-capture-*.json` 索引，回读验证后移除检查点。独立桌面程序的结果仍在其指定输出目录。
- `--capture-id <编号>` 或管理员 `GET /api/sources/erp/captures/{capture_id}` 返回完整采集结果。各组 `files` 记录原件路径和 SHA256。开发日志先按 `capture_id` 找 `capture.saved`，再用同条事件的 `operation_id` 回查登录、导出和轮询。日志受保留期约束，业务证据以原件和采集归档为准。
- CLI/API 保存批量归档后，单独把订单级清洗结果写入 `runtime_dir/erp-clean.sqlite3`。订单主键是采集编号和文本订单号。来源观察记录保留四类归属及原结果行号（从 1 起算，0 表示只有候选名单记录）。一致的订单财务数据共用一个版本。多值 `item ID` 拆分、去重并排序为父商品候选，原值留在采集归档。不同的财务值保留多个版本并记录冲突，空值和零各自保留。清洗数据库属于可重建的订单级对账结果，定价继续从独立合同取得明细、SKU 映射和在线证据。
- 清洗状态与采集完整性分别报告。清洗失败仍可查询已确认的原始归档，开发日志记录 `capture.clean_failed`。运行 `PYTHONPATH=backend .venv/bin/python -m app.capture_erp --capture-id <编号> --rebuild-clean` 可离线重建该批。历史采集可按需重放，历史原件和清单保留原有内容。
- `storage_verified=true`仅表示文件写入回读一致；SHA256用于原件核对，不是业务字段、权限或真实有效价验收。按订单查询返回归档中已有的候选名单、订单财务行及明细证据，不扫描原始 Excel。
- 离线回归核对批量入口、分批与任务绑定、来源范围、失败处理、归档读取及桌面独立构建。模拟测试通过不代表真实接口验收。
- 采集不修改业务订单或真实提交开关；筛选条件、来源版本及展示表按当前合同记录。

## 独立桌面程序

用 `scripts/build_erp_desktop.py` 从批量采集源码生成独立脚本。运行仅依赖 httpx 和 openpyxl，不读取项目 `.env`；密码由操作者交互输入，仅留在进程内存。每轮输出独立目录，保存原始 XLSX 及 `result.json`；默认每类试抓 20 笔，用 `--all` 全量。

## 销售员筛选与财务核对表

在 WSL 项目根目录执行 `.venv/bin/python scripts/run_erp_capture.py --salesperson 莫雯舒 --all`，可在当前 ERP 账号可见范围内导出该销售员四类来源的候选订单和财务明细。

采集器先在 `/Api/daysell/selectSellman` 按 `userName` 精确匹配唯一人员。亏损池传 `username=姓名`，核对原始名单的「销售员」列。禁止发货页的销售员下拉框用 `userID` 作选项值，订单列表请求传 `shopUsernameid=人员ID`，每页用返回行的 `shopUsername` 核对姓名。四类财务表都逐行核对「销售员」。名单销售员缺失或不符时，该来源停止导出。财务表归属不符时，保留原件并标记该批失败。

人工核对时选同一平台、集团、订单状态和销售员。亏损池的财务复核选「待处理」，日期和差额留空。禁止发货列表勾选「禁止发货」。

订单核对表以人工订单财务导出的列序为基准，新增财务列及亏损池独有字段追加，后者带“亏损池·”前缀。仅对已返回的财务行按订单号附加信息，缺列留证；原始 XLSX 保持原样。订单级财务完整不代表存在 SKU 明细亏损或完整在线定价证据。

## 每日费用配置（2026-11-23）

采集工作流和人工算价均在读取业务数据前检查销售系统费用及物流。每批只绑定启动时的当日配置：已成功来源同日复用，失败或无可用记录的来源由后续批次重试。原件、内容版本、检查时间及批次引用见[计算与来源](calculation-and-sources.md)。不读取旧表或其他费率覆盖；离线回放保留原始证据，不换成今天的费用。
