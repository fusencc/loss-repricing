"""以订单财务和两份原始导出核对在线变体，保留物理行引用供独立证据回查。"""

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import re
from typing import NamedTuple, get_args
import warnings
from xml.etree.ElementTree import ParseError, iterparse
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string
from openpyxl.xml.constants import SHEET_MAIN_NS
from openpyxl.utils.exceptions import InvalidFileException

from app.adapters.erp_pricing import BulkCapture, PricingEvidence, decimal_text
from app.adapters.normalization import identity_text, sku_text
from app.domain.types import Site


EXPORT_MAPPING_VERSION = "erp-sales-exports-v1"
ERP_COLUMNS = ("订单编号", "平台", "店铺", "商品SKU列表", "订单数量", "SKU修正毛利")
SALES_COLUMNS = ("店铺名称", "站点", "产品ID", "商品ID", "本地子sku", "平台子sku")
SUPPORTED_SITES = frozenset(get_args(Site))
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 128
ROW_TAG = f"{{{SHEET_MAIN_NS}}}row"
CELL_TAG = f"{{{SHEET_MAIN_NS}}}c"
ROW_NUMBER = re.compile(r"[1-9][0-9]{0,5}")
CELL_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,5})")
REASONS = {
    "mapped": "已匹配唯一在线变体，稳定明细 ID 与亏损另行核实",
    "invalid_order_id": "明细订单号缺失或不是原始文本",
    "order_not_in_capture": "明细订单不在本次采集范围",
    "finance_scope_mismatch": "订单财务行的订单号、平台或集团与本次采集范围不符",
    "conflicting_finance_identity": "财务来源间的店铺或父产品范围不一致",
    "missing_finance_identity": "采集订单缺少一致的店铺或父产品范围",
    "erp_detail_scope_mismatch": "明细平台或店铺与该采集订单不一致",
    "missing_erp_sku": "ERP 明细缺少本地子 SKU",
    "online_variant_not_found": "同店和父产品范围内找不到对应的本地子 SKU",
    "incomplete_online_variant": "匹配的销售行缺少在线商品标识、平台子 SKU 或已支持站点",
    "ambiguous_online_variant": "同店、父产品和本地子 SKU 匹配到多个在线变体",
    "evidence_export_coverage_mismatch": "补充证据的明细引用未完整对应本单的原始导出行",
    "evidence_target_mismatch": "补充证据的在线商品与原始导出匹配结果不一致",
    "evidence_identity_conflict": "同一导出变体关联了不同的规范店铺、仓配或主 SKU",
    "evidence_quantity_mismatch": "补充亏损对应件数与 ERP 明细导出不一致或无法核实",
}


class ObservedTarget(NamedTuple):
    shop: str
    site: str
    product_id: str
    item_id: str
    platform_sku: str


def _text(value, validator=identity_text):
    if not isinstance(value, str) or len(value) > 256:
        return None
    try:
        return validator(value)
    except ValueError:
        return None


def _product_ids(value):
    if not isinstance(value, str):
        return frozenset()
    # ERP 订单表的 item ID 是父产品值集合。禁止按排列位置与商品明细对号入座。
    tokens = re.split(r"[,，;；\s]+", value.strip())
    return frozenset(tokens) if all(_text(token) for token in tokens) else frozenset()


def _quantity(value):
    # 订单数量仅作对账和补充证据一致性检查。保留十进制，不从金额列生成亏损。
    try:
        quantity = decimal_text(str(value))
    except ValueError:
        return None
    return str(quantity) if quantity > 0 else None


def _sku_margin_status(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return "blank"
    # Excel 数值单元格可以用科学计数法存储。只识别是否为有限数，不消费金额。
    if type(value) in (int, float):
        return "present" if Decimal(str(value)).is_finite() else "invalid"
    try:
        decimal_text(str(value).strip())
    except ValueError:
        return "invalid"
    return "present"


def _check_layout(sheet, label):
    # 有些 ERP 导出把维度写成 A1。先检查工作表 XML 的实际坐标，避免按稀疏行号扩展空行。
    row = column = 0
    with sheet._get_source() as source:
        for event, element in iterparse(source, events=("start", "end")):
            if event == "end":
                element.clear()
                continue
            if element.tag == ROW_TAG:
                number = element.get("r", str(row + 1))
                if not ROW_NUMBER.fullmatch(number) or not row < int(number) <= MAX_ROWS:
                    raise ValueError(f"{label}实际坐标超限或行号无序")
                row, column = int(number), 0
            elif element.tag == CELL_TAG:
                address = element.get("r")
                next_column = column + 1
                if address is not None:
                    match = CELL_ADDRESS.fullmatch(address)
                    if not match or int(match[2]) != row:
                        raise ValueError(f"{label}实际坐标超限或单元格行号不一致")
                    next_column = column_index_from_string(match[1])
                if not column < next_column <= MAX_COLUMNS:
                    raise ValueError(f"{label}实际坐标超限或列号无序")
                column = next_column


def _xlsx_rows(content, columns, label):
    workbook = None
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > MAX_EXPANDED_BYTES:
                raise ValueError(f"{label}展开体积超限，请分开核对")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style.*", category=UserWarning)
            workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        if len(workbook.sheetnames) != 1 or len(workbook.worksheets) != 1:
            raise ValueError(f"{label}须为单工作表原始导出")
        sheet = workbook.worksheets[0]
        _check_layout(sheet, label)
        sheet.reset_dimensions()
        rows = sheet.iter_rows(max_col=MAX_COLUMNS)
        header = tuple(cell.value for cell in next(rows, ()))
        if any(header.count(column) != 1 for column in columns):
            raise ValueError(f"{label}缺少约定列或列名重复")
        positions = {column: header.index(column) for column in columns}
        for number, row in enumerate(rows, start=2):
            if not any(cell.value is not None for cell in row):
                continue
            selected = {column: row[i] for column, i in positions.items()}
            if any(cell.data_type == "f" for cell in selected.values()):
                raise ValueError(f"{label}第 {number} 行约定字段含公式，请提供原始导出")
            yield number, {column: cell.value for column, cell in selected.items()}
    except (BadZipFile, InvalidFileException, KeyError, OSError, ParseError) as exc:
        raise ValueError(f"{label}不是可读取的 XLSX 导出") from exc
    finally:
        if workbook is not None:
            workbook.close()


def _catalog(sales_variants, digest):
    lookup = defaultdict(dict)
    for number, row in _xlsx_rows(sales_variants, SALES_COLUMNS, "销售在线变体表"):
        shop, product = map(_text, (row["店铺名称"], row["产品ID"]))
        local_sku = _text(row["本地子sku"], sku_text)
        if not all((shop, product, local_sku)):
            raise ValueError("销售在线变体表存在无法定位范围的行，请核对店铺、产品ID和本地子SKU")
        site, item = map(_text, (row["站点"], row["商品ID"]))
        platform_sku = _text(row["平台子sku"], sku_text)
        target = (
            ObservedTarget(shop, site, product, item, platform_sku)
            if site in SUPPORTED_SITES and item and platform_sku else None
        )
        # 同一销售身份的重复行只保存首个证据引用。原文件摘要仍可回查全部记录。
        lookup[shop, product, local_sku].setdefault(target, f"sales-export-{digest}-r{number}")
    return lookup


def map_exports(capture: BulkCapture, erp_details, sales_variants):
    detail_hash, sales_hash = (sha256(content).hexdigest() for content in (erp_details, sales_variants))
    catalog = _catalog(sales_variants, sales_hash)
    memberships, identities = defaultdict(list), defaultdict(set)
    out_of_scope_orders = set()
    for group in capture.groups:
        candidate_ids = set(group.candidate_order_ids)
        bloc = group.scope_bloc()
        for order_id in group.candidate_order_ids:
            memberships[order_id].append(group.source)
        for row in group.rows:
            if row.orderid not in candidate_ids:
                continue
            if (row.orderid != row.fields.order_id or row.fields.platform.casefold() != "tiktok"
                    or bloc is None or row.fields.bloc != bloc):
                out_of_scope_orders.add(row.orderid)
                continue
            identities[row.orderid].add((_text(row.fields.shop), _product_ids(row.fields.product_hint)))

    rows, targets = [], defaultdict(set)
    for number, row in _xlsx_rows(erp_details, ERP_COLUMNS, "ERP 商品明细表"):
        order_id = _text(row["订单编号"])
        local_sku = _text(row["商品SKU列表"], sku_text)
        source_groups = list(dict.fromkeys(memberships.get(order_id, [])))
        if order_id in out_of_scope_orders:
            target, refs, code = None, [], "finance_scope_mismatch"
        else:
            target, refs, code = _match(row, order_id, local_sku, source_groups, identities.get(order_id, set()), catalog)
        margin_status = _sku_margin_status(row["SKU修正毛利"])
        rows.append({
            "order_id": order_id, "detail_ref": f"erp-detail-{detail_hash}-r{number}", "row_number": number,
            "erp_sku": local_sku, "quantity": _quantity(row["订单数量"]), "sources": source_groups,
            "sku_loss_present": margin_status == "present",
            "sku_loss_invalid": margin_status == "invalid",
            "target": target._asdict() if target else None, "sales_refs": refs,
            "code": code, "reason": REASONS[code],
        })
        if target:
            targets[target].add(order_id)

    mapped = sum(row["target"] is not None for row in rows)
    return {
        "version": EXPORT_MAPPING_VERSION,
        "hashes": {"erp_details": detail_hash, "sales_variants": sales_hash},
        "summary": {
            "detail_rows": len(rows), "mapped_rows": mapped, "unresolved_rows": len(rows) - mapped,
            "unique_targets": len(targets), "cross_order_targets": sum(len(orders) > 1 for orders in targets.values()),
            "sku_loss_present_rows": sum(row["sku_loss_present"] for row in rows),
            "sku_loss_invalid_rows": sum(row["sku_loss_invalid"] for row in rows),
        },
        "rows": rows,
        "evidence_conflicts": [],
    }


def _match(row, order_id, local_sku, sources, identities, catalog):
    if not order_id:
        return None, [], "invalid_order_id"
    if not sources:
        return None, [], "order_not_in_capture"
    if len(identities) > 1:
        return None, [], "conflicting_finance_identity"
    shop, products = next(iter(identities), (None, ()))
    if not shop or not products:
        return None, [], "missing_finance_identity"
    platform = _text(row["平台"])
    if not platform or platform.casefold() != "tiktok" or _text(row["店铺"]) != shop:
        return None, [], "erp_detail_scope_mismatch"
    if not local_sku:
        return None, [], "missing_erp_sku"

    candidate, reference = None, None
    for product in sorted(products):
        for target, ref in catalog.get((shop, product, local_sku), {}).items():
            if target is None:
                return None, [ref], "incomplete_online_variant"
            if candidate is not None and target != candidate:
                # 两个不同的变体已能证明歧义。保留这两个定位，避免按 ERP 行复制整组目录。
                return None, [reference, ref], "ambiguous_online_variant"
            candidate, reference = target, ref
    if candidate is None:
        return None, [], "online_variant_not_found"
    return candidate, [reference], "mapped"


def _mapping_target(mapping):
    key = mapping.key
    return ObservedTarget(mapping.erp_shop, key.site, key.product_id, key.item_id, key.sku)


def corroborate_evidence(evidence_data, export_mapping):
    """核对文件覆盖、商品与数量。独立证据提供稳定明细 ID、亏损和价格合同。"""
    evidence = PricingEvidence.model_validate(evidence_data)
    rows_by_order = defaultdict(dict)
    for row in export_mapping["rows"]:
        rows_by_order[row["order_id"]][row["detail_ref"]] = row
    has_unlocated = None in rows_by_order

    errors = export_mapping["evidence_conflicts"]

    def issue(order_id, code, ref=None, **comparison):
        errors.append({"order_id": order_id, "detail_ref": ref, "code": code, "reason": REASONS[code], **comparison})

    prepared = []
    canonical = defaultdict(set)
    for order in evidence.orders:
        rows = rows_by_order.get(order.order_id, {})
        refs = {line.detail_ref for line in order.lines}
        if has_unlocated or not rows or refs != rows.keys() or len(refs) != len(order.lines):
            issue(order.order_id, "evidence_export_coverage_mismatch")
            prepared.append(order.model_copy(update={"details_complete": False}))
            continue
        lines = []
        for line in order.lines:
            row = rows[line.detail_ref]
            if line.mapping is not None:
                target = _mapping_target(line.mapping)
                if not row["target"] or target != ObservedTarget(**row["target"]):
                    issue(
                        order.order_id, "evidence_target_mismatch", line.detail_ref,
                        export_target=row["target"], evidence_target=target._asdict(),
                        sales_refs=row["sales_refs"],
                    )
                    line = line.model_copy(update={"mapping": None})
                else:
                    key = line.mapping.key
                    canonical[target].add((key.shop_id, key.warehouse, line.mapping.main_sku))
            if line.loss is not None and (
                row["quantity"] is None or line.loss.quantity != Decimal(row["quantity"])
            ):
                # 保留两边的原件数量以便复核，金额证据仍须与导出行件数一致。
                issue(
                    order.order_id, "evidence_quantity_mismatch", line.detail_ref,
                    export_quantity=row["quantity"], evidence_quantity=str(line.loss.quantity),
                )
                line = line.model_copy(update={"loss": None})
            lines.append(line)
        prepared.append(order.model_copy(update={"lines": lines}))

    conflicting = {target for target, keys in canonical.items() if len(keys) > 1}
    orders = []
    for order in prepared:
        lines = []
        for line in order.lines:
            if line.mapping and _mapping_target(line.mapping) in conflicting:
                issue(order.order_id, "evidence_identity_conflict", line.detail_ref)
                line = line.model_copy(update={"mapping": None})
            lines.append(line)
        orders.append(order.model_copy(update={"lines": lines}))
    return evidence.model_copy(update={"orders": orders})
