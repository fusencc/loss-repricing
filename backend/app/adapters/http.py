"""可插入经审计数据合同的 HTTP 边界。公司字段由合同明示，默认关闭真实请求。

这是live定价任务的通用合同适配器，不是管理员ERP默认批量下载入口。
collect读取四候选、在线状态和当日汇率并构造Snapshot；来源请求及字段须由合同提供。
本适配器仍拒绝export异步合同；独立的ERP批量下载在erp_bulk_capture.py，
不代表这里已接通四组来源、在线价格/七日销量/保护历史或完整可执行定价合同。
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from urllib.parse import urlsplit

import httpx

from app.adapters.base import LookupResult, SourceUnavailable, SubmitResult
from app.adapters.excel import VAT_UPLOAD_COLUMNS, XLSX_MIME, price_upload_workbook
from app.adapters.normalization import (
    FX_FIELDS, LINE_CONSTANTS, LINE_FIELDS, LOOKUP_FIELDS, LOOKUP_OPTIONAL, ONLINE_CONSTANTS,
    ONLINE_FIELDS, SAFE_LABEL, fx_rates, identity_text, mapped, money, online_state, read_path, source_line,
    source_record_issue, source_stamp, source_time, valid_path, variant_key,
)
from app.adapters.sales_auth import SalesAuth
from app.adapters.transport_privacy import private_request
from app.config import Settings
from app.domain.engine import REQUIRED_SOURCES, SITE_CURRENCIES
from app.domain.types import Decision, SiteContract, Snapshot, VariantKey


SOURCES = ("loss_allocatable", "loss_pending", "blocked_allocatable", "blocked_pending")
assert set(SOURCES) == REQUIRED_SOURCES
COLLECTIONS = (*SOURCES, "online", "fx")
UPLOAD_PATH = "/Api/api-tiktok-sales/operationLog/priceUpload"
PRICE_BASIS = "actual_including_vat"
TIMEOUT = httpx.Timeout(30, connect=5)
BAD_DATA = (KeyError, IndexError, ValueError, TypeError, AttributeError, ArithmeticError)
LOOKUP_ERRORS = (*BAD_DATA, SourceUnavailable)
CREDENTIAL_FIELDS = {"authorization", "cookie", "token", "accesstoken", "refreshtoken", "apikey",
                     "clientsecret", "password", "secret"}
PLACEHOLDER = re.compile(r"{([a-z_][a-z0-9_]*)}")
ROW_REFERENCE = re.compile(r"第\s*([0-9]+)\s*行")
ROW_NOT_FOUND = re.compile(r"(?:上传完成\s*[,，]\s*)?第\s*([0-9]+)\s*行数据不存在在线列表[;；。]?")


def _utcnow():
    return datetime.now(timezone.utc)


def _label(value):
    return isinstance(value, str) and SAFE_LABEL.fullmatch(value) is not None


def _task_label(value):
    if type(value) is int and value >= 0:
        value = str(value)
    return value if _label(value) else None


def _origin(service):
    if not isinstance(service, dict) or not isinstance(service.get("base_url"), str):
        raise ValueError("服务地址未配置")
    address = service["base_url"]
    parsed = urlsplit(address)
    if (
        not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or any(char.isspace() for char in address) or "\\" in address
        or not (parsed.scheme == "https" or (
            parsed.scheme == "http" and service.get("allow_http_for_internal") is True
        ))
    ):
        raise ValueError("服务地址必须为已授权的服务源")
    return str(httpx.URL(address)).rstrip("/")


def _template(value, variables, used=None):
    if isinstance(value, list):
        return [_template(item, variables, used) for item in value]
    if isinstance(value, dict):
        if any(re.sub(r"[^a-z]", "", key.lower()) in CREDENTIAL_FIELDS for key in value):
            raise ValueError("请求参数不得配置凭据")
        return {key: _template(item, variables, used) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    names = PLACEHOLDER.findall(value)
    if used is not None:
        used.update(names)
    whole = PLACEHOLDER.fullmatch(value)
    if whole:
        return variables[whole[1]]
    without_placeholders = PLACEHOLDER.sub("", value)
    if "{" in without_placeholders or "}" in without_placeholders:
        raise ValueError("请求占位符无效")
    return PLACEHOLDER.sub(lambda match: str(variables[match[1]]), value)


def _request_parts(spec, variables, used=None):
    params = _template(spec.get("params", {}), variables, used)
    body = _template(spec.get("json", {}), variables, used)
    return params, body


def _fields(spec, required, optional=frozenset(), static=frozenset()):
    fields = spec.get("field_map")
    constants = spec.get("constants", {})
    if not isinstance(fields, dict) or not isinstance(constants, dict):
        raise ValueError("缺少字段映射")
    targets = set(fields) | set(constants)
    if (
        not required <= targets <= required | optional or set(fields) & set(constants)
        or not set(constants) <= static or not all(valid_path(path) for path in fields.values())
    ):
        raise ValueError("字段映射未齐备")


def _check_request(spec, services, variables):
    if not isinstance(spec, dict) or spec.get("enabled", True) is not True or not _label(spec.get("version")):
        raise ValueError("未配置或未启用")
    if spec.get("service") not in services or spec.get("method") not in {"GET", "POST"}:
        raise ValueError("请求服务或方法未确认")
    path = spec.get("path")
    if (
        not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
        or any(char in path for char in "\\?#{}") or any(char.isspace() for char in path)
    ):
        raise ValueError("接口必须是服务内相对路径")
    if (
        not isinstance(spec.get("params", {}), dict) or not isinstance(spec.get("json", {}), dict)
        or (spec["method"] == "GET" and spec.get("json"))
    ):
        raise ValueError("请求参数配置无效")
    used = set()
    _request_parts(spec, variables, used)
    return used


def _check_pagination(spec):
    paging = spec.get("pagination")
    if paging is None:
        return
    if not isinstance(paging, dict):
        raise ValueError("分页设置无效")
    page_key, limit_key = paging.get("page_key"), paging.get("limit_key", "limit")
    destination = spec.get("json" if spec["method"] == "POST" else "params", {})
    if (
        not _label(page_key) or not _label(limit_key) or page_key == limit_key
        or page_key in destination or limit_key in destination
        or type(paging.get("limit")) is not int or not 1 <= paging["limit"] <= 10000
        or type(paging.get("max_pages")) is not int or not 1 <= paging["max_pages"] <= 1000
        or not valid_path(paging.get("total_path"))
    ):
        raise ValueError("分页参数未确认")


def _check_collection(name, spec, services):
    _check_request(spec, services, {"business_date": "2026-01-01"})
    if not all(valid_path(spec.get(key)) for key in ("as_of_path", "business_date_path")):
        raise ValueError("来源日期映射未齐备")
    if "export" in spec:
        raise ValueError("异步导出任务绑定尚未接入")
    if name == "fx":
        _fields(spec, FX_FIELDS)
        if spec.get("pagination") is not None:
            raise ValueError("汇率来源必须为单个已确认对象")
    else:
        if not valid_path(spec.get("records_path")):
            raise ValueError("缺少记录列表路径")
        if name == "online":
            _fields(spec, ONLINE_FIELDS, static=ONLINE_CONSTANTS)
        else:
            _fields(spec, LINE_FIELDS, static=LINE_CONSTANTS)
        _check_pagination(spec)


def _check_submit(spec, services):
    if (
        not isinstance(spec, dict) or spec.get("verified") is not True or not _label(spec.get("version"))
        or "vendor" not in services or not _label(spec.get("file_field", "file"))
        or ("task_id_path" in spec and not valid_path(spec["task_id_path"]))
    ):
        raise ValueError("上传合同未确认")
    labels = spec.get("shop_labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("缺少模板店名映射")
    for shop_id, label in labels.items():
        identity_text(shop_id)
        identity_text(label)
    if len(set(labels.values())) != len(labels):
        raise ValueError("模板店名映射冲突")
    if "single_data_row_number" in spec and (
        type(spec["single_data_row_number"]) is not int or spec["single_data_row_number"] < 1
    ):
        raise ValueError("上传行号口径无效")
    header = spec.get("execution_id_header")
    if "execution_id_header" in spec and (
        not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]+", header)
        or header.lower() in {"authorization", "host", "cookie", "connection", "transfer-encoding"}
        or header.lower().startswith(("content-", "proxy-"))
    ):
        raise ValueError("执行标识请求头无效")


def _check_lookup(spec, services):
    variables = dict.fromkeys((*VariantKey.model_fields, "execution_id", "task_id"), "test-id")
    used = _check_request(spec, services, variables)
    if (
        spec.get("verified") is not True or not {"shop_id", "product_id"} <= used
        or not used & {"item_id", "sku"}
        or type(spec.get("max_age_seconds")) is not int or spec["max_age_seconds"] <= 0
    ):
        raise ValueError("实际价查询合同未确认")
    _fields(spec, LOOKUP_FIELDS, LOOKUP_OPTIONAL)
    if "rejected_status" in spec and (
        not _label(spec["rejected_status"]) or "row_status" not in spec["field_map"]
        or not set(spec["field_map"]) & {"execution_id", "task_id"}
    ):
        raise ValueError("拒绝回执缺少绑定证据")


class HttpAdapter:
    mode = "live"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._enabled = settings.live_submit_enabled
        self._erp_token = settings.erp_token
        self._erp_credentials = None
        # 凭据从独立环境变量或本机.env读取，不写入数据合同、业务快照或日志。
        # 登录合同依据衡哲开放文档 /474419940e0，已于2026-11-05实测正式环境。
        if (
            settings.erp_open_api_url in {
                "http://103.198.125.2:16777/open", "http://103.198.125.5:15672/open",
            }
            and settings.erp_username and settings.erp_password.get_secret_value()
        ):
            self._erp_credentials = {
                "base_url": settings.erp_open_api_url,
                "username": settings.erp_username,
                "password": settings.erp_password.get_secret_value(),
            }
        self._client = client if client is not None else httpx.Client(
            timeout=TIMEOUT, verify=True, follow_redirects=False, trust_env=False,
        )
        self._vendor_auth = SalesAuth(settings, self._client)
        self._services = {}
        self._collections = {}
        self._sites = {}
        self._profit_versions = {}
        self._submit = None
        self._lookup = None
        self._versions = {}
        self._collection_missing = []
        self._execution_missing = []
        try:
            config = json.loads(settings.live_contract_path.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict) or config.get("verified") is not True:
                self._collection_missing.append("真实数据合同尚未确认")
                return
        except (OSError, ValueError):
            self._collection_missing.append("未加载有效的真实数据合同")
            return
        self._load_contract(config)

    def _load_contract(self, config):
        services = config.get("services")
        for service in ("erp", "vendor"):
            try:
                origin = _origin(services[service])
            except (KeyError, TypeError, ValueError, httpx.InvalidURL):
                continue
            if service == "erp":
                configured = bool(self._erp_token) or (
                    self._erp_credentials is not None
                    and self._erp_credentials["base_url"] == origin + "/open"
                )
            else:
                configured = self._vendor_auth.configured and self._vendor_auth.origin == origin
            if configured:
                self._services[service] = origin

        collections = config.get("collections")
        for name in COLLECTIONS:
            try:
                spec = collections[name]
                _check_collection(name, spec, self._services)
            except BAD_DATA:
                self._collection_missing.append(f"未配置或未确认来源 {name} 的请求、凭据及字段映射")
            else:
                self._collections[name] = spec
                self._versions[name] = spec["version"]

        sites = config.get("sites")
        if isinstance(sites, dict):
            for site, spec in sites.items():
                try:
                    contract = SiteContract.model_validate(dict(spec, site=site))
                    if (
                        spec.get("verified") is not True or contract.price_step <= 0
                        or contract.upload_column not in VAT_UPLOAD_COLUMNS or not _label(contract.version)
                        or not _label(spec.get("online_profit_model_version")) or not contract.warehouses
                    ):
                        continue
                    for warehouse in contract.warehouses:
                        identity_text(warehouse)
                except BAD_DATA:
                    continue
                self._sites[site] = contract
                self._profit_versions[site] = spec["online_profit_model_version"]
                self._versions[f"online_profit_model.{site}"] = spec["online_profit_model_version"]
                self._versions[f"site.{site}"] = contract.version
        if not self._sites:
            self._execution_missing.append("缺少已审计站点的上传列及完整在线利润模型版本")

        submit = config.get("submit")
        try:
            _check_submit(submit, self._services)
        except BAD_DATA:
            self._execution_missing.append("缺少已确认的真实上传合同、销售凭据或模板店名映射")
        else:
            self._submit = submit
            self._versions["submit"] = submit["version"]

        lookup = config.get("lookup")
        try:
            _check_lookup(lookup, self._services)
        except BAD_DATA:
            self._execution_missing.append("缺少已确认的实际含税价格查询合同")
        else:
            self._lookup = lookup
            self._versions["lookup"] = lookup["version"]

    def readiness(self) -> dict:
        missing = self._collection_missing + self._execution_missing
        return {
            "contract_ready": not missing,
            "missing": missing,
            "verified_sites": sorted(self._sites),
            "source_versions": dict(self._versions),
        }

    def _request(self, method, service, path, *, headers=None, **kwargs):
        if service not in self._services:
            raise SourceUnavailable("来源服务或凭据尚未配置")
        try:
            context = private_request.set(True)
            try:
                if service == "vendor":
                    # 请求前取得有效令牌，业务失败时保持单次提交。
                    token = self._vendor_auth.get_token()
                else:
                    if not self._erp_token:
                        # 使用本轮登录所得令牌；不复用Apifox面板或其他系统的令牌。
                        login = self._client.post(
                            self._erp_credentials["base_url"] + "/system/innerOpen/login",
                            json={key: self._erp_credentials[key] for key in ("username", "password")},
                            timeout=TIMEOUT, follow_redirects=False,
                        )
                        login.raise_for_status()
                        payload = login.json()
                        if payload.get("success") is not True or payload.get("code") != 200:
                            raise SourceUnavailable("ERP开放接口登录未成功")
                        token = payload["data"].get("accessToken") or payload["data"].get("token")
                        if not isinstance(token, str) or not token.strip():
                            raise SourceUnavailable("ERP开放接口登录未返回有效令牌")
                        self._erp_token = re.sub(r"^Bearer\s+", "", token, flags=re.IGNORECASE).strip()
                    token = self._erp_token
                response = self._client.request(
                    method, self._services[service] + path,
                    headers={"Authorization": f"Bearer {token}", **(headers or {})},
                    timeout=TIMEOUT, follow_redirects=False, **kwargs,
                )
                response.raise_for_status()
                return response.json(parse_float=Decimal)
            finally:
                private_request.reset(context)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, *BAD_DATA):
            # 不携带响应、请求 URL、令牌或上游异常链。提交请求的异常由上层归为未知。
            raise SourceUnavailable("来源接口暂不可用或返回格式无效") from None

    def _fetch(self, spec, variables, *, page=None):
        params, body = _request_parts(spec, variables)
        if page is not None:
            paging = spec["pagination"]
            destination = body if spec["method"] == "POST" else params
            destination.update({paging["page_key"]: page, paging.get("limit_key", "limit"): paging["limit"]})
        args = {"params": params}
        if spec["method"] == "POST":
            args["json"] = body
        return self._request(spec["method"], spec["service"], spec["path"], **args)

    def _list_source(self, spec, variables, business_date):
        paging = spec.get("pagination")
        as_of = total = None
        rows = []
        for page in range(1, (paging["max_pages"] if paging else 1) + 1):
            payload = self._fetch(spec, variables, page=page if paging else None)
            try:
                stamp = source_stamp(payload, spec, business_date)
            except BAD_DATA:
                raise SourceUnavailable("列表来源缺少本轮业务日期或采集时间") from None
            try:
                records = read_path(payload, spec["records_path"])
                if not isinstance(records, list):
                    raise ValueError("记录不是列表")
                if paging is None:
                    return stamp, records
                page_total = read_path(payload, paging["total_path"])
                if type(page_total) is not int or page_total < 0 or len(records) > paging["limit"]:
                    raise ValueError("分页计数无效")
                if page == 1:
                    as_of, total = stamp, page_total
                if stamp != as_of or page_total != total:
                    raise ValueError("分页快照变化")
                rows.extend(records)
                if len(rows) == total:
                    return as_of, rows
                if not records or len(rows) > total:
                    raise ValueError("分页不完整")
            except BAD_DATA:
                raise SourceUnavailable("列表记录格式或分页完整性检查失败") from None
        raise SourceUnavailable("来源分页达到合同页数上限，仍有未获取记录")

    def _normalize_records(self, name, spec, records):
        online = name == "online"
        paginated = spec.get("pagination") is not None
        normalized, issues, seen = [], [], set()
        for number, record in enumerate(records, start=1):
            try:
                row = online_state(record, spec, self._profit_versions) if online else source_line(record, spec, name)
                normalized.append(row)
            except BAD_DATA:
                try:
                    row = source_record_issue(record, spec, name, number)
                except BAD_DATA:
                    # 候选行须能同时定位到订单明细和在线目标，在线行还须能定位共享销量的主 SKU。
                    raise SourceUnavailable(f"来源 {name} 第 {number} 条记录的商品或订单明细身份无法核实") from None
                issues.append(row)
            if paginated:
                # 在线分页的记录键包含仓配。缺该字段无法证明不同页是不同在线记录。
                if online and row.key.warehouse is None:
                    raise SourceUnavailable(f"来源 {name} 的分页记录缺少可核实仓配")
                # 字段坏行也不能用来填满重复页的计数。
                identity = row.key.grouping() if online else (row.key.platform, row.key.shop_id, row.row_id)
                if identity in seen:
                    raise SourceUnavailable(f"来源 {name} 的分页记录重复")
                seen.add(identity)
        return normalized, issues

    def collect(self, business_date: date) -> Snapshot:
        if self._collection_missing:
            raise SourceUnavailable("；".join(self._collection_missing))
        variables = {
            "business_date": business_date.isoformat(),
        }
        lines, online, issues, stamps = [], [], [], []
        for name in (*SOURCES, "online"):
            spec = self._collections[name]
            stamp, records = self._list_source(spec, variables, business_date)
            normalized, row_issues = self._normalize_records(name, spec, records)
            if name == "online":
                # 同一上传对象存在无法解释的在线记录时，不可让另一份在线记录建立接管/修正基线。
                held_targets = {}
                for issue in row_issues:
                    held_targets.setdefault(issue.key.target_identity(), issue)
                held_main_skus = defaultdict(set)
                for state in normalized:
                    target = state.key.target_identity()
                    if target in held_targets:
                        held_main_skus[target].add(state.main_sku)
                    else:
                        online.append(state)
                # 同一目标的记录若主 SKU 不同，所有销量范围均需核对。附加范围在该目标首条异常中记一次。
                for target, main_skus in held_main_skus.items():
                    issue = held_targets[target]
                    issue.related_main_skus = sorted(main_skus - {issue.main_sku})
            else:
                lines.extend(normalized)
            issues.extend(row_issues)
            stamps.append(stamp)

        spec = self._collections["fx"]
        payload = self._fetch(spec, variables)
        try:
            stamps.append(source_stamp(payload, spec, business_date))
            fx_date, rates = fx_rates(payload, spec["field_map"], business_date)
        except BAD_DATA:
            raise SourceUnavailable("汇率来源日期、汇率方向基准或数据格式无效") from None
        # 保留所有来源中最早的采集时间，避免本地取数时间覆盖旧来源的原始时间。
        return Snapshot(
            as_of=min(stamps), business_date=business_date, fx_date=fx_date, fx_rates=rates,
            lines=lines, online=online, contracts=self._sites, complete_sources=list(SOURCES),
            source_versions=self._versions, source_issues=issues,
        )

    def submit(self, execution_id: str, decision: Decision) -> SubmitResult:
        if not self._enabled or not self.readiness()["contract_ready"]:
            return SubmitResult("rejected", message="真实提交未开启或合同尚未确认")
        site = self._sites.get(decision.key.site)
        if (site is None or not decision.ready or site.upload_column != decision.upload_column
                or decision.key.warehouse not in site.warehouses):
            return SubmitResult("rejected", message="商品未通过站点合同或算价校验")
        spec = self._submit
        try:
            variant_key(decision.key.model_dump())
            book = price_upload_workbook(decision, spec["shop_labels"][decision.key.shop_id])
            header = spec.get("execution_id_header")
            if header and (not _label(execution_id) or not execution_id.isascii()):
                raise ValueError("执行标识无效")
        except BAD_DATA:
            return SubmitResult("rejected", message="缺少模板店名映射或上传字段无效")

        # 只发一次 POST，一文件一商品；网络错误可能发生在服务已经接收之后。
        try:
            with book:
                payload = self._request(
                    "POST", "vendor", UPLOAD_PATH,
                    headers={header: execution_id} if header else None,
                    files={spec.get("file_field", "file"): ("price.xlsx", book, XLSX_MIME)},
                )
        except SourceUnavailable:
            return SubmitResult("unknown", message="上传回执未获得，须查询实际价")
        return self._upload_result(payload, spec)

    @staticmethod
    def _upload_result(payload, spec):
        unknown = SubmitResult("unknown", message="上传回执无法确认，须查询实际价")
        if not isinstance(payload, dict) or type(payload.get("resp_code")) is not int or payload["resp_code"] != 0:
            return unknown
        message = payload.get("resp_msg")
        if not isinstance(message, str):
            return unknown
        # 历史已见错误是字符串。只有行号合同已确认且已知错误唯一指向本文件数据行时才拒绝。
        message = message.strip()
        if ROW_REFERENCE.search(message):
            matched = ROW_NOT_FOUND.fullmatch(message)
            if matched and matched[1] == str(spec.get("single_data_row_number")):
                return SubmitResult("rejected", message="上传数据行在在线列表中不存在")
            return unknown
        # 2026-11-17单SKU真实响应为“上传完成,”，随后出现同SKU同目标价任务。
        # 精确接受该已见回执；含逐行错误或其他未知文字仍不能因此放行。
        if message not in {"", "上传完成", "上传完成,", "上传成功", "成功"}:
            return unknown
        task_id = None
        if "task_id_path" in spec:
            try:
                task_id = _task_label(read_path(payload, spec["task_id_path"]))
            except BAD_DATA:
                pass
        return SubmitResult("accepted", task_id, "上传已受理，待回读实际价格")

    def lookup(self, execution_id: str, task_id: str | None, key: VariantKey) -> LookupResult:
        spec = self._lookup
        if spec is None:
            return LookupResult("unknown", message="缺少已确认的实际含税价格查询合同")
        try:
            variant_key(key.model_dump())
            variables = key.model_dump() | {"execution_id": execution_id, "task_id": task_id}
            payload = self._fetch(spec, variables)
            fields = mapped(payload, spec["field_map"])
            if variant_key(fields["key"]) != key:
                raise ValueError("复合商品身份不匹配")
            # 若查价合同返回本次执行或任务标识，已知标识冲突时不能用该回执终结执行。
            echoes = [(fields[name], variables[name]) for name in ("execution_id", "task_id")
                      if fields.get(name) is not None and variables[name] is not None]
            if any(_task_label(received) != expected for received, expected in echoes):
                raise ValueError("回执标识与本次执行冲突")
            if "rejected_status" in spec and fields["row_status"] == spec["rejected_status"]:
                if echoes:
                    return LookupResult("rejected", message="已收到与本次执行绑定的拒绝回执")
                raise ValueError("拒绝回执无法绑定")
            if fields["price_basis"] != PRICE_BASIS:
                raise ValueError("实际价口径不匹配")
            # 依据设计中的同币种有效价核验：站点身份不能替代回读金额币种证据。
            # currency 为内部规范名，真实字段须由合同映射；缺失时保持未知，不猜测或换汇。
            if fields["currency"] != SITE_CURRENCIES[key.site]:
                raise ValueError("实际价币种与站点币不一致")
            actual_price = money(fields["actual_price"])
            observed_at = source_time(fields["observed_at"])
            # “当前有效价”需要合同明确可接受的来源年龄；本项目不替公司猜一个统一阈值。
            # 过期观测只能说明过去的价，不得结束未知执行或解锁新提交。
            if (_utcnow() - observed_at).total_seconds() > spec["max_age_seconds"]:
                raise ValueError("实际价观测已超过合同有效期")
            effective = fields.get("effective_at")
            effective_at = source_time(effective) if effective is not None else None
            if actual_price <= 0 or (effective_at is not None and effective_at > observed_at):
                raise ValueError("实际价或生效时间无效")
        except LOOKUP_ERRORS:
            return LookupResult("unknown", message="缺少同商品身份、同口径与有效来源时间的价格证据")
        return LookupResult("accepted", actual_price, observed_at, effective_at, PRICE_BASIS, "取得实际含税价格")
