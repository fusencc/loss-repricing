"""把已确认合同的字段路径转换为内部模型。保留原始币种和价格单位，由领域层算价。"""
import json
from datetime import date, datetime
from decimal import Decimal
import re

from app.domain.engine import BEIJING
from app.domain.types import (
    CandidateSourceIssue, FeeModel, OnlineSourceIssue, OnlineState, SourceLine, VariantKey, VariantReference,
)


KEY_FIELDS = frozenset(f"key.{field}" for field in VariantKey.model_fields)
LINE_FIELDS = KEY_FIELDS | {
    "row_id", "order_id", "main_sku", "quantity", "loss_amount", "currency",
    "basis", "allocation_reliable", "fee_scope", "abnormal",
}
ONLINE_FIELDS = KEY_FIELDS | {
    "main_sku", "currency", "retail_price", "marketing_price", "current_margin", "fee_scope",
    "observed_at", "sales_7d", "sales_start", "sales_end", "sales_complete", "history_known",
    "last_increase_at", "pending_submission", "protected", "activity_locked", "sellable",
    "fee_model.revenue_factor", "fee_model.fixed_cost", "fee_model.version", "fee_model.verified",
}
FX_FIELDS = frozenset({"fx_date", "rates"})
LOOKUP_FIELDS = KEY_FIELDS | {"actual_price", "currency", "observed_at", "price_basis"}
LOOKUP_OPTIONAL = frozenset({"effective_at", "row_status", "task_id", "execution_id"})
# 请求日期只是查询条件。动态字段及空值必须从已确认的响应字段取证。
LINE_CONSTANTS = {"basis", "allocation_reliable", "fee_scope", "abnormal"} | KEY_FIELDS
ONLINE_CONSTANTS = {"fee_scope"} | KEY_FIELDS
BOOL_FIELDS = {
    "allocation_reliable", "abnormal", "sales_complete", "history_known", "pending_submission",
    "protected", "activity_locked", "sellable",
}
CURRENCY = re.compile(r"[A-Z]{3}")
SAFE_LABEL = re.compile(r"[\w.-]{1,128}")


def valid_path(value):
    return isinstance(value, str) and bool(value) and all(value.split("."))


def read_path(obj, path):
    for part in path.split("."):
        if isinstance(obj, list) and part.isascii() and part.isdecimal():
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def mapped(obj, field_map, constants=None):
    values = dict(constants or {})
    values.update({target: read_path(obj, path) for target, path in field_map.items()})
    result = {}
    for target, value in values.items():
        parent, dot, child = target.partition(".")
        if dot:
            result.setdefault(parent, {})[child] = value
        else:
            result[parent] = value
    return result


def source_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("业务日期格式无效")
    return date.fromisoformat(value)


def source_time(value):
    # 仅接受带偏移量的 ISO 8601 来源时间，不自动猜测毫秒/秒或当地时区。
    if not isinstance(value, str):
        raise ValueError("来源时间格式无效")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("来源时间缺少时区")
    return result


def source_stamp(payload, spec, business_date):
    as_of = source_time(read_path(payload, spec["as_of_path"]))
    reported_date = source_date(read_path(payload, spec["business_date_path"]))
    if reported_date != business_date or as_of.astimezone(BEIJING).date() != business_date:
        raise ValueError("来源业务日期无效")
    return as_of


def _identifier(value, forbidden):
    # 数字 ID 必须由提供方以原文返回，避免把已丢失前导零的数值转换成假身份。
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or any(not char.isprintable() for char in value)
        or any(marker in value for marker in forbidden)
    ):
        raise ValueError("商品或订单标识无效")
    return value


def identity_text(value):
    return _identifier(value, ("://", "?", "#"))


def sku_text(value):
    # 平台SKU是原始商家文本；可打印边界空格、@和#均保留，以便逐字核对。
    # 订单/产品/店铺ID仍使用独立严格校验，不裁剪SKU或合并不同原始文本。
    if (not isinstance(value,str) or not value.strip()
            or any(not char.isprintable() for char in value)
            or any(marker in value for marker in ("://", "?"))):
        raise ValueError("平台SKU文本无效")
    return value


def _variant_values(values, sku_validator):
    return {name: (sku_validator if name == "sku" else identity_text)(value) for name, value in values.items()}


def variant_key(values):
    return VariantKey.model_validate(_variant_values(values, identity_text))


def money(value):
    # HTTP JSON 小数在反序列化时已直接进入 Decimal。布尔值也不能充作金额。
    if type(value) not in (str, int, Decimal):
        raise ValueError("金额格式无效")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("金额不是有限数")
    return result


def _validate_common(values):
    values["key"] = variant_key(values["key"])
    values["main_sku"] = identity_text(values["main_sku"])
    if not isinstance(values["currency"], str) or not CURRENCY.fullmatch(values["currency"]):
        raise ValueError("币种无效")
    identity_text(values["fee_scope"])
    if any(type(values[field]) is not bool for field in BOOL_FIELDS & values.keys()):
        raise ValueError("业务状态必须是布尔值")


def _order_detail_identity(order_id, detail_id):
    order_id, detail_id = identity_text(order_id), identity_text(detail_id)
    # 上游明细号可能只在订单内唯一，JSON 数组保留两个标识的文本前导零。
    return order_id, json.dumps([order_id, detail_id], ensure_ascii=False, separators=(",", ":"))


def source_record_issue(record, spec, source, record_number):
    fields = (KEY_FIELDS - {"key.warehouse"}) | ({"main_sku"} if source == "online" else {"order_id", "row_id"})
    values = mapped(
        record,
        {name: path for name, path in spec["field_map"].items() if name in fields},
        {name: value for name, value in spec.get("constants", {}).items() if name in fields},
    )
    # 仓配不参与稳定目标身份。异常证据保留空值，仍可定位商品及订单明细。
    warehouse_path = spec["field_map"].get("key.warehouse")
    try:
        raw = read_path(record, warehouse_path) if warehouse_path else spec.get("constants", {}).get("key.warehouse")
        warehouse = identity_text(raw)
    except (KeyError, IndexError, TypeError, ValueError):
        warehouse = None
    # 已观察到平台子 SKU 含 #。这里只定位挂起范围，正常算价、提交及回读沿用已确认的键合同。
    key = VariantReference.model_validate({**_variant_values(values["key"], sku_text), "warehouse": warehouse})
    if source == "online":
        return OnlineSourceIssue(source=source, record_number=record_number, key=key,
                                 main_sku=identity_text(values["main_sku"]))
    order_id, row_id = _order_detail_identity(values["order_id"], values["row_id"])
    return CandidateSourceIssue(source=source, record_number=record_number, key=key, order_id=order_id, row_id=row_id)


def source_line(record, spec, source):
    values = mapped(record, spec["field_map"], spec.get("constants"))
    _validate_common(values)
    values["order_id"], values["row_id"] = _order_detail_identity(values["order_id"], values["row_id"])
    values["quantity"] = money(values["quantity"])
    values["loss_amount"] = money(values["loss_amount"])
    # 来源版本和原始明细行定位足以回溯。保留负号、件数、原币和粒度，引用不含下载链接。
    return SourceLine(
        source=source, source_ref=f"{source}/{spec['version']}/{values['row_id']}", **values
    )


def online_state(record, spec, fee_versions):
    values = mapped(record, spec["field_map"], spec.get("constants"))
    _validate_common(values)
    values["observed_at"] = source_time(values["observed_at"])
    values["sales_start"] = source_date(values["sales_start"])
    values["sales_end"] = source_date(values["sales_end"])
    if values["last_increase_at"] is not None:
        values["last_increase_at"] = source_time(values["last_increase_at"])
    for name in ("retail_price", "marketing_price", "current_margin"):
        if name != "marketing_price" or values[name] is not None:
            values[name] = money(values[name])

    fee_values = values["fee_model"]
    if all(value is None for value in fee_values.values()):
        values["fee_model"] = None
    else:
        version = fee_values["version"]
        if not isinstance(version, str) or not SAFE_LABEL.fullmatch(version):
            raise ValueError("一次毛利模型版本无效")
        if type(fee_values["verified"]) is not bool:
            raise ValueError("一次毛利模型审核标记无效")
        # 系数无量纲，固定成本为站点币。经核对的站点版本才可参与利润复核。
        values["fee_model"] = FeeModel(
            revenue_factor=money(fee_values["revenue_factor"]),
            fixed_cost=money(fee_values["fixed_cost"]),
            version=version,
            verified=fee_values["verified"] and fee_versions.get(values["key"].site) == version,
        )
    return OnlineState.model_validate(values)


def fx_rates(data, field_map, business_date):
    values = mapped(data, field_map)
    fx_date = source_date(values["fx_date"])
    rates = {key: money(value) for key, value in values["rates"].items()}
    # 合同必须已确认方向为每 1 CNY 兑换的币种单位。旧工作簿的反向汇率不能直接沿用。
    if (
        fx_date != business_date or rates.get("CNY") != Decimal(1)
        or any(not CURRENCY.fullmatch(key) or value <= 0 for key, value in rates.items())
    ):
        raise ValueError("汇率日期或 CNY 基准无效")
    return fx_date, rates
