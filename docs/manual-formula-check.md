# 手动公式核对

`scripts/manual_formula_check.py` 复用正式领域引擎，离线输出输入快照、判定和完整 `trace`。不读取凭据、不联网、不连接业务数据库、不提交价格。业务公式仍以 [核心计算规范](calculation-and-sources.md) 为准，本工具不改变公式、字段合同或规则版本。

## 修改合成参数并运行

在脚本顶部 `PARAMETERS` 中修改负亏损金额、明细件数、含税零售价、营销价、汇率、完整费用模型系数及上传档位。金额用十进制字符串；营销价 `None` 仅代表明确无折扣，不代表未知。默认样例为 MY 直发，费用固定成本已是 MYR/件、除数为1，亏损金额为CNY/明细。脚本的来源完整、无保护和已确认模型标记均是合成假设，不能作为真实执行证据。

在项目根目录运行：

```bash
PYTHONPATH=backend .venv/bin/python scripts/manual_formula_check.py
```

默认样例独立手算：明细亏2 CNY、数量1，每CNY兑换0.6 MYR，单件补亏1.2 MYR；当前营销价10、零售价20，成交比例0.5。初步补亏价11.2；完整利润模型为0.8×营销价−10，1%最低通过价为10/0.79。无参考，向上进0.01上传档位后上传价25.32，预计营销价12.66、单件利润0.128 MYR。

## 保存及回放

默认终端输出分步骤的中文说明，解释原始输入、单位、单件化、换汇、折扣、1%利润门槛、参考上限、上传档位及最终结果。说明直接使用引擎已保存的值，不重新计算金额；未记录值明确标示，不视为0。需要完整JSON终端输出时加 `--json`，其中同样包含 `中文计算说明`。

需要留存时，显式提供新文件路径（父目录需已存在），保存的JSON包含完整输入、判定和中文说明：

```bash
PYTHONPATH=backend .venv/bin/python scripts/manual_formula_check.py --output /tmp/manual-formula-result.json
PYTHONPATH=backend .venv/bin/python scripts/manual_formula_check.py --snapshot /absolute/path/snapshot.json --output /tmp/snapshot-formula-result.json
```

`--snapshot` 接收独立的规范 `Snapshot` JSON，不是ERP原始响应或批次包装JSON；按其 `as_of` 时点回放，不拿当天汇率、费用或时间补旧快照。可以保留真实快照中的仓配、费用成本除数、参考价和来源版本。此模式只复现历史时点计算，不证明当前仍可执行。

保存文件包括完整输入及判定，可能包含业务敏感数据，应保存在受控目录且不入Git。工具拒绝覆盖已有输出，不写入业务批次、执行意图或生效历史。全部判定 `ready=true` 且非空时退出码为0，否则为1；`ready`仅表示该输入可算价。

## 看哪些结果

- `unit_loss`、`station_loss`：单件原币亏损幅度及站点币补亏额。
- `current_actual`、`discount`：当前实际价和成交比例，不是减价百分比。
- `trace.online_profit`：初步补亏价、初始利润及利润率、最低通过价、在线利润校正额和最终利润证据。
- `trace.price_limit`：存在参考上限时的压减过程。
- `target_actual`、`upload_price`、`trace.rounded_actual`、`predicted_profit`：目标实际价、含税上传价、预计进档后实际价和单件完整在线利润。
- `ready`、`reason`：可否算价及具体原因。缺条件保留失败原因，不伪造价格。

## 正式流程是否存储

正式 `PricingService` 流程在取数、评估后把首次规范快照写入 `Batch.snapshot`，并保存商品判定及计算证据。刷新和重跑证据另行留存，不覆盖首次快照。抓取字段齐全仍须通过来源、身份、日期和费用等检查；字段齐全不等于已提交，算价通过也不等于已生效。单独运行纯领域引擎不自动存储；本工具只在指定 `--output` 时保存独立核对文件。
