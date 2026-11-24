"""离线手动算价：复用正式引擎，不联网、不连接数据库、不提交价格。"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from app.domain.engine import evaluate
from app.domain.types import Snapshot


# 手动修改这里，金额全部用字符串。以下均为合成参数，不是公司已验收费用。
PARAMETERS = {
    "loss_amount": "-2",       # ERP修正一次毛利，CNY/明细，保留负号
    "quantity": "1",           # 该明细的可售件数
    "retail_price": "20",      # 当前含VAT零售价，MYR/件
    "marketing_price": "10",   # 当前营销价，MYR/件；None仅表示明确无折扣
    "myr_per_cny": "0.6",      # 每1 CNY兑换的MYR，不是反方向报价
    "revenue_factor": "0.8",   # 完整费用模型收入系数，无量纲，不是百分数80
    "fixed_cost": "10",        # 完整费用模型固定成本，MYR/件
    "price_step": "0.01",      # 合成上传档位，非真实站点合同
    "rounding": "ROUND_UP",
}


def build_sample(parameters=None, now=None):
    """构造单条直发合成快照；四来源齐备、无保护仅是手算假设，不是真实取证。"""
    p = PARAMETERS if parameters is None else parameters
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone(timedelta(hours=8))).date()
    key = dict(platform="synthetic", shop_id="MANUAL", site="MY", product_id="001",
               item_id="0001", sku="SAMPLE", warehouse="direct")
    return Snapshot.model_validate(dict(
        as_of=now, business_date=day, fx_date=day,
        fx_rates={"CNY": "1", "MYR": p["myr_per_cny"]},
        fx_rate_unit="currency_per_cny",
        lines=[dict(row_id="sample-row", order_id="sample-order", source="loss_allocatable",
                    key=key, main_sku="SAMPLE-MAIN", quantity=p["quantity"],
                    loss_amount=p["loss_amount"], currency="CNY", basis="line",
                    source_ref="synthetic/manual-formula")],
        online=[dict(key=key, main_sku="SAMPLE-MAIN", currency="MYR",
                     retail_price=p["retail_price"], marketing_price=p["marketing_price"],
                     fee_scope="normal", observed_at=now, history_known=True,
                     profit_check=dict(overseas_stock="0", mode="direct", department="SYNTHETIC",
                                       inventory_observed_at=now, source_ref="synthetic/manual-fees",
                                       model=dict(revenue_factor=p["revenue_factor"],
                                                  fixed_cost=p["fixed_cost"], fixed_cost_divisor="1",
                                                  version="synthetic-manual-fees-v1", verified=True)))],
        contracts={"MY": dict(site="MY", verified=True, price_step=p["price_step"],
                              rounding=p["rounding"], upload_column="税后价格",
                              version="synthetic-manual-contract-v1", warehouses=["direct"])},
        complete_sources=["loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending"],
        source_versions={"input": "synthetic-manual-v1"},
    ))


def calculate_report(snapshot, evaluation_time):
    # 只调用纯计算入口：利润、换汇、参考上限及档位均由正式引擎决定，不另写手算算法。
    return {
        "purpose": "离线公式核对，不代表已提交或已生效",
        "evaluation_time": evaluation_time.isoformat(),
        "snapshot": snapshot.model_dump(mode="json"),
        "decisions": [d.model_dump(mode="json") for d in evaluate(snapshot, evaluation_time)],
    }


def explain_report(report):
    """只解释引擎已保存的结果，不用展示小数重新计算价格或利润。"""
    def value(data, key):
        result = data.get(key)
        return "未记录或未计算（不代表0）" if result is None else str(result)

    snapshot = report["snapshot"]
    lines = ["手动公式核对（离线，只计算，不提交价格）",
             "输入类型：" + ("合成样例，参数不代表真实公司费用。" if report.get("input_kind") == "synthetic"
                          else "指定规范快照，按原采集时点回放，不代表当前可执行。"),
             f"评估时间：{report['evaluation_time']}；业务日期：{snapshot['business_date']}",
             "金额单位：除特别注明外，价格和利润均为站点币/件；利润率是比值，0.01表示1%。",
             "存储说明：默认只显示；指定 --output 才保存独立核对文件，不写业务数据库或执行历史。"]
    if not report["decisions"]:
        lines.append("本次没有生成商品判定，请检查快照中的候选和在线商品范围。")
    for index, decision in enumerate(report["decisions"], 1):
        trace = decision.get("trace", {})
        profit = trace.get("online_profit", {})
        key = decision["key"]
        currency = trace.get("station_currency", "站点币（未记录）")
        lines.extend(["", f"商品{index}：子SKU {key['sku']}；店铺 {key['shop_id']}；站点 {key['site']}；具体商品ID {key['item_id']}",
                      "一、原始输入和使用口径"])
        # 来源值原样展示，不能把单件、明细与整单金额混为一个口径。
        for row in snapshot["lines"]:
            if row["key"] == key:
                basis = {"unit": "单件金额", "line": "明细金额", "order": "整单金额"}[row["basis"]]
                lines.append(f"候选明细 {row['row_id']}：ERP修正一次毛利={row['loss_amount']} {row['currency']}，"
                             f"数量={row['quantity']}件，口径={basis}。负号表示亏损。")
        for online in snapshot["online"]:
            if online["key"] == key:
                marketing = online["marketing_price"]
                lines.append(f"当前含税零售价={online['retail_price']} {online['currency']}/件；"
                             f"营销价={marketing if marketing is not None else '明确无营销价，使用零售价'}。")
                model = (online.get("profit_check") or {}).get("model", {})
                lines.append(f"完整在线利润模型：G(P)={value(model, 'revenue_factor')}×P"
                             f"−{value(model, 'fixed_cost')}÷{value(model, 'fixed_cost_divisor')}。"
                             "P是营销实际价；成本除数用于换成站点币，不能用亏损汇率替代。")
        lines.extend([f"快照汇率={json.dumps(snapshot['fx_rates'], ensure_ascii=False)}；"
                      f"报价方向={snapshot['fx_rate_unit']}（currency_per_cny为原币/CNY；cny_per_currency为CNY/原币）。",
                      "二、亏损单件化、换汇与折扣",
                      f"选中亏损明细：{value(decision, 'selected_row_id')}。同一子SKU选最小有效单件亏损，不跨子SKU共用。",
                      f"单件亏损幅度 u={value(decision, 'unit_loss')} {value(decision, 'loss_currency')}/件。"
                      "单件金额取绝对值；明细金额取绝对值后除以件数；整单金额还须确认可可靠归属。",
                      f"换汇后补亏额 Δ={value(decision, 'station_loss')} {currency}/件。按快照报价方向换算，不重复扣费用。",
                      f"当前实际价 P={value(decision, 'current_actual')}；成交比例 d=P/含税零售价={value(decision, 'discount')}。"
                      "例如0.5表示按零售价的50%成交，不是上传价。",
                      "三、补亏和完整在线利润校验",
                      f"初步补亏价 T0=P+Δ={value(profit, 'initial_target')}。",
                      f"初步补亏价的单件利润={value(profit, 'initial_profit')}；利润率={value(profit, 'initial_margin')}。",
                      f"仓配模型={ {'direct': '直发', 'overseas': '海外仓'}.get(profit.get('mode'), '未记录')}；"
                      f"海外库存={value(profit, 'overseas_stock')}件；部门={value(profit, 'department')}。",
                      f"1%最低通过营销价 B=固定成本÷成本除数÷(收入系数−0.01)={value(profit, 'break_even')}。"
                      "收入系数小于等于0.01时需按引擎边界规则判断，不能直接套除法。",
                      f"在线利润校正额 C=max(0,B−T0)={value(profit, 'correction_amount')}。这是还需增加的营销价，不是利润额。",
                      f"本条是否强制满足1%：{ {True: '是，没有参考上限时执行1%要求', False: '否，参考上限优先，利润不足或为负仍如实记录'}.get(profit.get('minimum_margin_required'), '未记录，不能推定通过')}。",
                      f"参考上限证据：{json.dumps(trace['price_limit'], ensure_ascii=False)}" if "price_limit" in trace
                      else "参考上限：本次判定未记录参考压减证据。",
                      "四、上传档位和最终结果",
                      f"目标营销价 T={value(decision, 'target_actual')}；未进档上传价 Uraw=T/d={value(trace, 'raw_upload_price')}。"])
        contract = snapshot["contracts"].get(key["site"], {})
        rounding = {"ROUND_UP": "向上取整", "ROUND_DOWN": "向下取整", "ROUND_HALF_UP": "四舍五入"}
        lines.extend([f"上传档位步长={value(contract, 'price_step')}；舍入方式={rounding.get(contract.get('rounding'), '未记录')}。"
                      "无参考还需保证1%最低通过档位；有参考以上限档位优先。",
                      f"最终含税上传建议价 U={value(decision, 'upload_price')} {currency}/件（只建议，未上传）。",
                      f"预计进档后营销价 P'=U×d={value(trace, 'rounded_actual')} {currency}/件。"
                      "此预测依赖平台保持成交比例，不能当作真实回读。",
                      f"预计每件完整在线利润 G(P')={value(decision, 'predicted_profit')} {currency}/件；"
                      f"预计利润率={value(profit, 'rounded_margin')}。",
                      f"档位后与目标实际价的绝对误差={value(trace, 'actual_price_error')}；允许误差={value(trace, 'allowed_error')}。",
                      f"结论：{'算价通过' if decision['ready'] else '未通过算价'}；原始业务状态={decision['status']}。",
                      f"具体原因：{decision['reason']}（原因代码：{decision['reason_code']}）。",
                      f"公式版本：{value(trace, 'formula_version')}。",
                      "证据边界：算价通过不代表已提交、接口受理或真实生效；未记录的值不能当作0。"])
        for warning in trace.get("warnings", []):
            lines.append(f"需要核实的告警：{warning}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="明确指定规范Snapshot JSON；按原采集时点回放，不补字段")
    parser.add_argument("--output", type=Path, help="可选保存完整输入与计算证据；必须是新文件，防止覆盖历史")
    parser.add_argument("--json", action="store_true", help="终端输出完整JSON（含中文解释）；默认输出中文分步说明")
    args = parser.parse_args()
    snapshot = Snapshot.model_validate_json(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else build_sample()
    report = calculate_report(snapshot, snapshot.as_of)
    report["input_kind"] = "specified-snapshot-replay" if args.snapshot else "synthetic"
    report["中文计算说明"] = explain_report(report)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        # 仅写用户明确指定的独立核对文件，不写业务数据库或执行历史。
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text if args.json else report["中文计算说明"])
    return 0 if all(d["ready"] for d in report["decisions"]) and report["decisions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
