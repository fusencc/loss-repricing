"""手动工具仅用合成输入；期望值独立手算，不构成真实费用验收。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


# 直接加载脚本模块，测试实际供手动运行的入口，不另建一份计算实现。
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manual_formula_check.py"
spec = importlib.util.spec_from_file_location("manual_formula_check", SCRIPT)
manual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manual)


def test_documented_manual_sample():
    """核对默认样例的单件化、换汇、1%门槛、折扣反推和档位后利润。"""
    snapshot = manual.build_sample()
    result = manual.calculate_report(snapshot, snapshot.as_of)["decisions"][0]
    # 明细亏2 CNY/1件×0.6=1.2 MYR；P=10，d=0.5。
    # B=10/(0.8-0.01)，上传ceil(B/0.5,0.01)=25.32，实价12.66，利润0.128。
    assert result["ready"]
    assert result["unit_loss"] == "2"
    assert result["station_loss"] == "1.2"
    assert result["upload_price"] == "25.32"
    assert result["predicted_profit"] == "0.128"
    assert result["trace"]["rounded_actual"] == "12.66"


def test_invalid_quantity_is_not_repaired():
    """数量为0不能完成明细单件化，应保留失败而非补成1件或生成上传价。"""
    snapshot = manual.build_sample({**manual.PARAMETERS, "quantity": "0"})
    result = manual.calculate_report(snapshot, snapshot.as_of)["decisions"][0]
    assert not result["ready"]
    assert result["upload_price"] is None


def test_cli_replays_snapshot_and_never_overwrites(tmp_path):
    """用临时文件验证历史回放、中文终端输出与JSON留证，重复运行不得覆盖旧证据。"""
    snapshot = manual.build_sample()
    source = tmp_path / "snapshot.json"
    source.write_text(snapshot.model_dump_json(), encoding="utf-8")
    output = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--snapshot", str(source), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # 默认终端面向人工阅读；完整证据保存在JSON，中文说明也随文件留存。
    assert "最终含税上传建议价 U=25.32" in result.stdout
    assert "不提交价格" in result.stdout
    saved = output.read_text(encoding="utf-8")
    report = json.loads(saved)
    assert report["input_kind"] == "specified-snapshot-replay"
    assert report["snapshot"] == snapshot.model_dump(mode="json")
    assert report["中文计算说明"] == result.stdout.rstrip("\n")
    # 原始输入完全相同也拒绝覆盖，保留第一次核对时的结果。
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_text(encoding="utf-8") == saved


def test_chinese_explanation_preserves_failure_and_unknown_values():
    """异常输入也要能读中文原因；未计算的价格明确未知，不展示为0或通过。"""
    snapshot = manual.build_sample({**manual.PARAMETERS, "quantity": "0"})
    report = manual.calculate_report(snapshot, snapshot.as_of)
    explanation = manual.explain_report(report)
    assert "未通过算价" in explanation
    assert "未记录或未计算（不代表0）" in explanation
    assert report["decisions"][0]["reason"] in explanation


def test_json_output_contains_chinese_explanation():
    """显式JSON模式便于程序读取，同时保留供人工核对的中文说明。"""
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["input_kind"] == "synthetic"
    assert "预计每件完整在线利润 G(P')=0.128" in report["中文计算说明"]
