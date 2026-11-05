"""公司批量改价模板。依据 docs/data-contract-evidence.md 第 4 节保留表头、位置与文本 ID。"""
from io import BytesIO

from openpyxl import Workbook

from app.adapters.normalization import identity_text
from app.domain.types import Decision


UPLOAD_HEADERS = (
    "店铺名", "产品ID", "商品ID", "税前价格", "税后价格",
    "海外仓税前价格", "直发仓税前价格", "海外仓税后价格", "直发仓税后价格",
)
VAT_UPLOAD_COLUMNS = frozenset(UPLOAD_HEADERS[index] for index in (4, 7, 8))
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def price_upload_workbook(decision: Decision, shop_label: str) -> BytesIO:
    price = decision.upload_price
    column = decision.upload_column
    if column not in VAT_UPLOAD_COLUMNS or price is None or not price.is_finite() or price <= 0:
        raise ValueError("模板列或上传价格无效")
    # XLSX 数值单元格精度有限。异常价格无法在模板原样表达时，在提交前关闭执行。
    significant_digits = "".join(str(digit) for digit in price.as_tuple().digits).rstrip("0")
    if len(significant_digits) > 15 or not -300 <= price.adjusted() <= 300:
        raise ValueError("上传价格超出模板数值精度")

    identifiers = [identity_text(value) for value in (shop_label, decision.key.product_id, decision.key.item_id)]
    if any(len(identifier) > 32767 for identifier in identifiers):
        raise ValueError("模板标识超出单元格长度")
    book = Workbook()
    sheet = book.active
    sheet.title = "DATA-0"
    sheet.append(UPLOAD_HEADERS)
    for index, identifier in enumerate(identifiers, start=1):
        # 显式使用文本单元格，保留前导零及精确 ID，也避免把来源文本解释为公式。
        sheet.cell(2, index, identifier).data_type = "s"
    sheet.cell(2, UPLOAD_HEADERS.index(column) + 1, price)
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return stream
