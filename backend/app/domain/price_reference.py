"""同 SKU 的可选营销参考价；有值时作为营销价上限，缺失不新增限制。"""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StrictStr, computed_field

from .money import MoneyRatio

Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Discount = Annotated[Decimal, Field(ge=0, lt=100, allow_inf_nan=False)]


def _marketing_ratio(sample):
    return (MoneyRatio(sample.tax_inclusive_price)
            * (MoneyRatio(Decimal(100)) - MoneyRatio(sample.discount_percent))
            / MoneyRatio(Decimal(100)))


class PriceReferenceSample(BaseModel):
    shop_name: StrictStr
    product_id: StrictStr
    item_id: StrictStr
    sales_7d: Annotated[StrictInt, Field(ge=0)]
    tax_inclusive_price: Price
    discount_percent: Discount

    @computed_field
    @property
    def marketing_price(self) -> Decimal:
        # 2026-11-20 用户确认：税后价×(100－折扣)%；保留半分，不套系统营销价舍入。
        return _marketing_ratio(self).decimal()


class SkuPriceReference(BaseModel):
    version: Literal['same-sku-top3-marketing-mean-v1'] = 'same-sku-top3-marketing-mean-v1'
    sku: StrictStr
    currency: StrictStr
    marketing_price: Price
    samples: Annotated[list[PriceReferenceSample], Field(min_length=3, max_length=3)]
    observed_at: AwareDatetime
    source_ref: StrictStr


def calculate_price_reference(samples: list[PriceReferenceSample], *, sku: str,
                              currency: str, observed_at: datetime,
                              source_ref: str) -> SkuPriceReference | None:
    """只取有销量的三个不同链接；并列按身份排序，不按价格挑低/高价。"""
    links = {}
    for sample in samples:
        if sample.sales_7d <= 0:
            continue
        # 一个父商品链接的多个同SKU子规格不能凑成多个参考；有歧义就省略。
        identity = sample.shop_name, sample.product_id
        if identity in links and links[identity] != sample:
            return None
        links[identity] = sample
    ranked = sorted(links.values(), key=lambda s: (-s.sales_7d, s.shop_name, s.product_id, s.item_id))
    if len(ranked) < 3:
        return None
    selected = ranked[:3]
    total = sum((_marketing_ratio(s) for s in selected), MoneyRatio(Decimal(0)))
    return SkuPriceReference(sku=sku, currency=currency,
        marketing_price=(total / MoneyRatio(Decimal(3))).decimal(),
        samples=selected, observed_at=observed_at, source_ref=source_ref)


def reference_upper_price(reference: SkuPriceReference | None, currency: str) -> MoneyRatio | None:
    """上限按三条来源精确比值取均值，避免循环小数展示值导致少进一档。"""
    if reference is None or reference.currency != currency:
        return None
    total = sum((_marketing_ratio(s) for s in reference.samples), MoneyRatio(Decimal(0)))
    return total / MoneyRatio(Decimal(3))


def describe_price_limit(upper: MoneyRatio, before: MoneyRatio | None, after: MoneyRatio) -> dict:
    return {'policy': 'sku-reference-priority-v2', 'upper_actual': str(upper.decimal()),
        'exact_upper_actual': {'numerator': str(upper.numerator), 'denominator': str(upper.denominator)},
        'unlimited_target_actual': str(before.decimal()) if before is not None else None,
        'applied': before is None or after < before,
        'reduction_amount': str((before - after).decimal()) if before is not None else None}


def saved_price_limit(trace: dict) -> MoneyRatio | None:
    """只核验本次明确采用上限的请求，不重新解释旧的仅参考历史。"""
    info = trace.get('price_limit')
    if info is None:
        return None
    if info.get('policy') != 'sku-reference-priority-v2':
        raise ValueError('参考上限规则版本未知')
    exact = info['exact_upper_actual']
    upper = MoneyRatio(Decimal(exact['numerator']), Decimal(exact['denominator']))
    if upper <= MoneyRatio(Decimal(0)):
        raise ValueError('参考上限不是正数')
    return upper
