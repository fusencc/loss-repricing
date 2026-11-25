from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext


def format_decimal(value: Decimal | str | None) -> str | None:
    """仅格式化报表数值，不改计算、执行或归档的精度。"""
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return value
    with localcontext(rounding=ROUND_HALF_UP):
        text = format(amount, '.2f').rstrip('0').rstrip('.')
    return '0' if text == '-0' else text
