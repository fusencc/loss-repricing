"""Decimal 金额比值：延迟除法，只在展示时转为有限小数。

依据：docs/calculation-and-sources.md 的单件化、换汇、档位和零毛利边界。
1/3 等循环小数不能先近似再决定档位或正负；这里保留 Decimal 分子/正分母，
有限小数的乘加按所需位数精确计算，比较使用交叉乘积，档位使用商余数。
不使用 float，不给毛利增加 epsilon，也不改变业务舍入方式。
"""
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from functools import total_ordering


PRICE_CONTEXT = Context(prec=50)
ONE = Decimal(1)


def _product(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(PRICE_CONTEXT) as context:
        context.prec = max(context.prec, len(left.as_tuple().digits) + len(right.as_tuple().digits))
        return left * right


def _sum(left: Decimal, right: Decimal) -> Decimal:
    # 对齐到最小指数并预留进位；禁止让调用者的全局 Decimal 精度影响判定。
    with localcontext(PRICE_CONTEXT) as context:
        context.prec = max(context.prec, max(left.adjusted(), right.adjusted())
                           - min(left.as_tuple().exponent, right.as_tuple().exponent) + 2)
        return left + right


@total_ordering
@dataclass(frozen=True, eq=False)
class MoneyRatio:
    numerator: Decimal
    denominator: Decimal = ONE

    def __post_init__(self):
        if not self.numerator.is_finite() or not self.denominator.is_finite() or self.denominator <= 0:
            raise ValueError("金额比值须为有限数且分母为正")

    def __add__(self, other: "MoneyRatio") -> "MoneyRatio":
        return MoneyRatio(_sum(_product(self.numerator, other.denominator),
                               _product(other.numerator, self.denominator)),
                          _product(self.denominator, other.denominator))

    def __sub__(self, other: "MoneyRatio") -> "MoneyRatio":
        return self + MoneyRatio(other.numerator.copy_negate(), other.denominator)

    def __mul__(self, other: "MoneyRatio") -> "MoneyRatio":
        return MoneyRatio(_product(self.numerator, other.numerator),
                          _product(self.denominator, other.denominator))

    def __truediv__(self, other: "MoneyRatio") -> "MoneyRatio":
        # 业务只除以正数量、正汇率、正零售价与档位；负或零的除数不猜测处理。
        return MoneyRatio(_product(self.numerator, other.denominator),
                          _product(self.denominator, other.numerator))

    def __eq__(self, other):
        if not isinstance(other, MoneyRatio):
            return NotImplemented
        return _product(self.numerator, other.denominator) == _product(other.numerator, self.denominator)

    def __lt__(self, other):
        if not isinstance(other, MoneyRatio):
            return NotImplemented
        return _product(self.numerator, other.denominator) < _product(other.numerator, self.denominator)

    def absolute(self) -> "MoneyRatio":
        return MoneyRatio(self.numerator.copy_abs(), self.denominator)

    def decimal(self) -> Decimal:
        """仅供保存和展示；业务分支必须在精确比值上比较，不读回近似值判断。"""
        with localcontext(PRICE_CONTEXT):
            return self.numerator / self.denominator

    def round_to_step(self, step: Decimal, rounding: str) -> Decimal:
        """正上传价按档位取商余数，精确识别整档及半档，不先除出近似小数。"""
        ticks = self / MoneyRatio(step)
        numerator, denominator = ticks.numerator, ticks.denominator
        with localcontext(PRICE_CONTEXT) as context:
            # 商的整数位及余数对齐位均完整保留；Decimal divmod 不引入循环小数。
            context.prec = max(context.prec, max(numerator.adjusted(), denominator.adjusted())
                               - min(numerator.as_tuple().exponent, denominator.as_tuple().exponent) + 2)
            whole, remainder = divmod(numerator, denominator)
            if rounding == "ROUND_UP":
                whole = _sum(whole, ONE) if remainder else whole
            elif rounding == "ROUND_HALF_UP":
                whole = _sum(whole, ONE) if _product(remainder, Decimal(2)) >= denominator else whole
            elif rounding != "ROUND_DOWN":
                raise ValueError("未支持的站点舍入方式")
            return _product(whole, step)
