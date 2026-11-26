# ERP 四类原版导出工具（WSL）

源码来自用户提供的 `erp-four-export.zip`，保留原版采集、重试、节流、冷却、锁和分页逻辑；只调整账号配置加载。未带入原包的账号配置、历史下载、日志、状态或 Windows 虚拟环境。

## 账号

自动读取本项目根目录 `.env` 的 `LR_ERP_USERNAME` / `LR_ERP_PASSWORD`，同名环境变量优先。账号不复制到工具目录，原包内置账号已移除。平台、平台 ID、集团也读取项目对应 `LR_ERP_CAPTURE_*` 配置。

可选的工具 `config.json` 仅用于其他采集参数，其中 username/password 不生效。原版默认请求间隔为 20 秒、禁止发货每页 100 条、明细每批 1000 单、请求上限 500 次；可通过命令行覆盖。**本工具按集团采集，不支持部门筛选。**

## 运行

在 `/home/developer/projects/loss-repricing` 下使用现有项目虚拟环境，无需安装原包的 Windows 环境：

```bash
# 先探测四类数量（亏损来源仍会下载名单 Excel，不导出订单明细）
.venv/bin/python tools/erp-four-export/erp_four_export.py probe

# 每类最多导出 20 单明细，候选名单仍完整采集
.venv/bin/python tools/erp-four-export/erp_four_export.py run --max-orders 20

# 四类全量
.venv/bin/python tools/erp-four-export/erp_four_export.py run

# 仅补抓禁止发货两类
.venv/bin/python tools/erp-four-export/erp_four_export.py run --only 3,4
```

`--only`：1=亏损可配货，2=亏损待处理，3=禁止发货可配货，4=禁止发货待处理。其他参数见 `--help`，例如 `--page-size 100 --interval 20 --batch-size 1000 --max-requests 500`。

默认输出到工具目录 `downloads/<时间戳>/`；`result.json` 记录请求数、耗时、候选数与文件位置，原始 Excel 在同一目录。运行状态和技术请求日志保存在工具目录 `state/`。

原版保留超时/5xx 有限重试、任务查询退避、跨进程节流、单实例锁、限流冷却和停止逻辑；与此前独立部门测试脚本行为不同。请避免与其他使用同一 ERP 账号的采集并发执行。仅下载 ERP 数据及创建下载任务，不上传价格、不推送钉钉。

原版会将数值订单号转为文本，可能无法恢复已丢失的精度；因此本工具输出只用于人工采集核对，不直接作为生产调价输入。真实数据、日志、状态和配置应仅本地保存，不提交 Git。

当前仅完成本地配置验证及帮助检查，未进行真实 ERP 请求。
