"""已保存 ERP 采集的只读算价与隔离文件对账。"""

from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapters.erp_export_mapping import corroborate_evidence, map_exports
from app.adapters.erp_pricing import BulkCapture, prepare_pricing
from app.db import transaction
from app.domain.engine import evaluate
from app.domain.types import Decision, Snapshot
from app.models import Batch
from app.services.erp_capture_store import CAPTURE_ID, load_capture, write_verified
from app.services.store import ERP_RECONCILIATION_PREFIX, stamp, write_decisions


PREVIEW_ID = re.compile(r'[a-f0-9]{32}')


def _prepare_preview(capture, evidence, exports, current_fx=None, order_preparation=None, online_preparation=None):
    mapping = None
    if exports is not None:
        capture = BulkCapture.model_validate(capture)
        mapping = map_exports(capture, *exports)
    if mapping is not None and evidence is not None:
        evidence = corroborate_evidence(evidence, mapping)
    preparation = prepare_pricing(capture, evidence, current_fx=current_fx, order_preparation=order_preparation, online_preparation=online_preparation)
    decisions = (
        evaluate(preparation.snapshot, preparation.evaluated_at)
        if preparation.snapshot is not None else []
    )
    report = preparation.report
    if order_preparation is not None:
        notes = defaultdict(list)
        for note in order_preparation['issues']:
            notes[note['order_id']].append(note)
        for issue in report['issues']:
            if issue['code'] == 'missing_order_details' and notes[issue['order_id']]:
                source_notes = notes[issue['order_id']]
                issue.update(code='native_order_preparation_pending',
                    reason='；'.join(note['reason'] for note in source_notes), native_preparation_issues=source_notes)
    if mapping is not None:
        report["export_mapping"] = mapping
        rows_by_order = defaultdict(list)
        for row in mapping["rows"]:
            if row["sources"]:
                rows_by_order[row["order_id"]].append(row)
        for issue in report["issues"]:
            rows = rows_by_order.get(issue["order_id"])
            if issue["code"] != "missing_order_details" or not rows:
                continue
            mapped_rows = sum(row["target"] is not None for row in rows)
            loss_present = sum(row["sku_loss_present"] for row in rows)
            loss_invalid = sum(row["sku_loss_invalid"] for row in rows)
            loss_status = (
                f"SKU 修正毛利 {loss_present}/{len(rows)} 行有数值，{loss_invalid} 行格式无效"
                if loss_present or loss_invalid else "SKU 修正毛利全空"
            )
            issue.update(
                code="export_detail_identity_pending",
                reason=f"已读 {len(rows)} 行，{mapped_rows} 行匹配在线变体；{loss_status}。仍需核实明细 ID 和算价依据。",
                export_detail_summary={
                    "detail_rows": len(rows), "mapped_rows": mapped_rows, "sku_loss_present_rows": loss_present,
                    "sku_loss_invalid_rows": loss_invalid,
                },
            )
    report["decisions"] = [decision.model_dump(mode="json") for decision in decisions]
    report["summary"].update(decisions=len(decisions), ready_prices=sum(decision.ready for decision in decisions))
    return preparation


def preview_pricing(capture, evidence=None, *, exports=None):
    return _prepare_preview(capture, evidence, exports).report


def _public_preview(artifact):
    report = dict(artifact)
    report["batch_input_available"] = report.pop("_snapshot", None) is not None
    return report


def save_pricing_preview(runtime_dir, capture_id, evidence=None, *, exports=None, current_fx=None, sales_source=None, native_source=None, order_preparation=None, order_headers_source=None, sales_window_source=None, local_history_source=None, platform_source=None, online_preparation=None, fixed_fees_source=None):
    capture = load_capture(runtime_dir, capture_id)
    if sales_source is not None and (
        sales_source['erp_capture_id'] != capture_id
        or sales_source['synthetic'] != bool(capture.get('synthetic', False))
        or sales_source['sample_only'] != bool(capture.get('sample_only', False))
    ):
        raise ValueError('销售来源与采集编号或样例属性不一致')
    if order_preparation is not None and (evidence is not None or exports is not None):
        raise ValueError('实时订单准备不能与文件对账输入混用')
    if (order_preparation is not None and sales_source is not None
            and order_preparation['references']['sales_source'] != sales_source['source_id']):
        raise ValueError('实时订单准备与销售来源引用不一致')
    if native_source is not None and (
        native_source['erp_capture_id'] != capture_id
        or native_source['synthetic'] != bool(capture.get('synthetic', False))
        or native_source['sample_only'] != bool(capture.get('sample_only', False))
        or (order_preparation is not None and order_preparation['references']['native_source'] != native_source['source_id'])
    ):
        raise ValueError('原生明细来源与采集、样本或订单准备引用不一致')
    for label, source in (('七日销量',sales_window_source),('本程序真实提价历史',local_history_source),('平台现态',platform_source),('固定费率',fixed_fees_source)):
        if source is not None and (source['erp_capture_id'] != capture_id
                or source['synthetic'] != bool(capture.get('synthetic', False))
                or source['sample_only'] != bool(capture.get('sample_only', False))):
            raise ValueError(label+'来源与采集或样本属性不一致')
    if order_headers_source is not None and (
        order_headers_source['erp_capture_id'] != capture_id
        or order_headers_source['synthetic'] != bool(capture.get('synthetic', False))
        or order_headers_source['sample_only'] != bool(capture.get('sample_only', False))
        or (order_preparation is not None and order_preparation['references']['order_headers'] != order_headers_source['source_id'])
    ):
        raise ValueError('订单归属来源与采集、样本或订单准备引用不一致')
    if online_preparation is not None:
        for name, source in (('sales',sales_source),('platform',platform_source),('window',sales_window_source),
                             ('local_history',local_history_source),('fixed_fees',fixed_fees_source)):
            expected = source['source_id'] if source else None
            if online_preparation['references'].get(name) != expected:
                raise ValueError('实时在线准备与'+name+'来源引用不一致')
    preparation = _prepare_preview(capture, evidence, exports, current_fx=current_fx,
        order_preparation=order_preparation, online_preparation=online_preparation)
    artifact = dict(preparation.report, preview_id=uuid4().hex)
    if sales_source is not None:
        # 来源观察可回查，不将原始在线／费率字段冒充已审核的完整定价输入。
        artifact['references']['sales_source'] = sales_source
    if fixed_fees_source is not None:
        artifact['references']['fixed_fees']=fixed_fees_source
    if platform_source is not None:
        artifact['references']['platform_current_state'] = platform_source
    if sales_window_source is not None:
        artifact['references']['sales_window'] = sales_window_source
    if local_history_source is not None:
        artifact['references']['local_price_history'] = local_history_source
    if order_headers_source is not None:
        artifact['references']['order_headers'] = order_headers_source
    if native_source is not None:
        artifact['references']['native_details'] = native_source
    if order_preparation is not None:
        path = Path(runtime_dir) / 'erp-order-preparations' / (uuid4().hex + '.json')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_verified(path, order_preparation)
        artifact['references']['order_preparation'] = {'artifact_file':str(path.resolve()),
            'sha256':sha256(path.read_bytes()).hexdigest(), 'version':order_preparation['version'],
            'summary':order_preparation['summary']}
    if online_preparation is not None:
        path = Path(runtime_dir) / 'erp-online-preparations' / (uuid4().hex + '.json')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_verified(path,online_preparation)
        artifact['references']['online_preparation'] = {'artifact_file':str(path.resolve()),
            'sha256':sha256(path.read_bytes()).hexdigest(),'version':online_preparation['version'],
            'online_records':len(online_preparation['online']),'issues':len(online_preparation['issues'])}
    snapshot = preparation.snapshot
    if snapshot is not None and snapshot.lines:
        # 回放快照与原报告一次持久化。缺汇率或没有可定位商品时，只保留准备报告。
        artifact["_snapshot"] = snapshot.model_dump(mode="json")
    directory = Path(runtime_dir) / "erp-pricing-previews"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_verified(directory / (artifact["preview_id"] + ".json"), artifact)
    return _public_preview(artifact)


def _load_pricing_artifact(runtime_dir, capture_id, preview_id):
    if not CAPTURE_ID.fullmatch(capture_id) or not PREVIEW_ID.fullmatch(preview_id):
        raise ValueError("采集编号或报告编号格式无效")
    path = Path(runtime_dir) / "erp-pricing-previews" / (preview_id + ".json")
    content = path.read_bytes()
    report = json.loads(content.decode("utf-8"))
    if not isinstance(report, dict) or report.get("preview_id") != preview_id:
        raise ValueError("报告文件结构或编号无效")
    if report.get("capture_id") != capture_id:
        raise FileNotFoundError("本轮采集没有该报告")
    return report, sha256(content).hexdigest()


def load_pricing_preview(runtime_dir, capture_id, preview_id):
    artifact, digest = _load_pricing_artifact(runtime_dir, capture_id, preview_id)
    return _public_preview(artifact), digest


def reconciliation_key(capture_id, preview_id):
    return f"{ERP_RECONCILIATION_PREFIX}{capture_id}:{preview_id}"


def find_preview_reconciliation(session, key, digest):
    result = session.execute(select(
        Batch.id, Batch.snapshot["erp_reconciliation"]["artifact_sha256"].as_string(),
    ).where(Batch.run_key == key, Batch.mode == "file")).one_or_none()
    if result is None:
        return None
    batch_id, saved_digest = result
    if saved_digest != digest:
        raise ValueError("报告内容与已保存对账批次的依据不一致")
    return batch_id


def save_preview_reconciliation(factory, config, capture_id, preview_id, digest):
    if config.adapter_mode != "file":
        raise ValueError("ERP 报告只能在文件对账环境入批")
    key = reconciliation_key(capture_id, preview_id)
    with factory() as session:
        if batch_id := find_preview_reconciliation(session, key, digest):
            return batch_id

    artifact, observed_digest = _load_pricing_artifact(config.runtime_dir, capture_id, preview_id)
    if observed_digest != digest:
        raise ValueError("报告内容在任务入队后已改变")
    snapshot = Snapshot.model_validate(artifact.get("_snapshot"))
    business_date = snapshot.business_date.isoformat()
    if (business_date != artifact["business_date"] or not snapshot.lines
            or snapshot.source_versions.get("erp_capture_id") != capture_id):
        raise ValueError("对账快照与原采集或业务日期不一致")

    # 保留原回放时点和当时的判定。入批只落档，不用当前时间或本地执行历史改写历史建议。
    decisions = [Decision.model_validate(value) for value in artifact["decisions"]]
    provenance = {field: artifact[field] for field in ("capture_id", "preview_id", "synthetic", "evaluated_at")}
    provenance["artifact_sha256"] = digest
    batch_id = str(uuid4())
    try:
        with transaction(factory) as session:
            session.add(Batch(
                id=batch_id, run_key=key, business_date=business_date, mode="file",
                status="completed", created_at=stamp(),
                snapshot={**snapshot.model_dump(mode="json"), "erp_reconciliation": provenance},
                summary=dict(Counter(decision.status for decision in decisions)),
            ))
            session.flush()
            write_decisions(session, batch_id, decisions, ready_hold="文件对账仅保留算价建议")
    except IntegrityError:
        # 租约恢复或并发插入由唯一键裁决。已提交的批次与商品记录属于同一事务。
        with factory() as session:
            if existing_id := find_preview_reconciliation(session, key, digest):
                return existing_id
        raise
    return batch_id
