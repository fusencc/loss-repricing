"""当前销售前端13924明确使用的店铺命名合同。"""
from typing import get_args

from app.domain.types import Site


SHOP_SITE_CONTRACT = 'sales-ui13924-shop-suffix-v1'


def site_from_shop_name(shop_name):
    # online组件的substrSite取店铺名最后两位；原始父产品site列通常为空。
    if not isinstance(shop_name, str) or len(shop_name) < 2:
        return None
    site = shop_name[-2:]
    return site if site in get_args(Site) else None
