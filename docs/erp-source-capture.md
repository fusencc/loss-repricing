# ERP只读数据采集

## 当前采集入口

默认批量入口读取 **亏损订单／禁止发货 × 可配货／待处理** 四类 TikTok 候选，集团取当前 ERP 配置。

### 1. 默认四类批量下载

- CLI `PYTHONPATH=backend .venv/bin/python -m app.capture_erp --progress` 和管理员 `POST /api/sources/erp/captures` 默认全量。可用 `source` 显式选择单类；CLI、独立桌面脚本使用 `--source`。
- 亏损两类用 `POST /Api/deficit/dowDeficitOrder` 下载原始名单，分别指定订单状态 `status=4/3`，均指定财务复核 `financecheck="3"`，排除自动放行等其他复核状态。复核待处理的 Excel 值可能为空白。
- 禁止发货两类用 `POST /Api/order/list`，按平台 ID、集团、`jzfh="1"` 和 `status=4/3` 分页，每页 10000。2026-11-11 已实测两类分别单页返回 2467、4431 条，等于各自响应总数且范围一致；减少翻页期间订单处理造成的名单漂移。超过单页仍分页并检查总数和页长。不加财务复核条件。订单号唯一性、平台、状态和禁止发货标记须通过检查才导出。整组直导实测混入非禁止发货订单。
- 四类名单都保留文本订单号。按来源分别用 `asyncOrderListDownload` 的 `orderIdList` 分批提交，默认每批最多 1000；按返回任务名和 ID 在任务列表绑定，最多轮询 20 次。每批记录请求、实际、缺失、超范围和重复行；状态变动单列记录。失败批不自动重提，来源失败不影响其他独立来源，账号限流停止整轮。
- 财务表按订单核对。重复财务行连同原件保留，所在来源标记财务冲突，采集与试抓均不报完成。重复业务列无法确定投影时，保留原件并报告来源错误。
- `candidate_order_ids` 是完整候选名单，`list_complete` 表示名单通过检查；`batches[].scope_check` 记录财务对账。两类亏损另留名单原始 XLSX 和财务核对表。文件名前缀是来源名。
- `--max-orders N` 仅限制**每类**财务导出数量，桌面默认每类 20 笔，可用 `--all` 全量。CLI 和桌面指定 `--salesperson` 时仅处理两类亏损，因为禁止发货来源尚无经验证的销售员筛选。
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
- demo使用合成数据；file读取规范快照，不是直接读取ERP原始采集JSON；live的`backend/app/adapters/http.py::HttpAdapter.collect`按独立合同读取四候选、在线状态、汇率并转换字段，仍拒绝异步export合同。
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

CLI 与桌面的 `--salesperson 姓名` 只适用于亏损来源。先调用 `/Api/daysell/selectSellman` 按 `userName` 精确匹配唯一人员，再按网页合同传 `username`，并核对下载名单的销售员列。筛选不会改变登录身份或账号可见范围。人工核对使用相同平台、集团、订单状态和销售员，财务复核选待处理，日期、差额留空。

订单核对表以人工订单财务导出的列序为基准，新增财务列及亏损池独有字段追加，后者带“亏损池·”前缀。仅对已返回的财务行按订单号附加信息，缺列留证；原始 XLSX 保持原样。订单级财务完整不代表存在 SKU 明细亏损或完整在线定价证据。
