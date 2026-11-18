# 当前销量查询计划

原全店真实主SKU方案已由用户明确取消。现行实现为 `backend/app/adapters/erp_goods_sales.py`，先近72小时历史过滤，再批量补充候选子SKU仓库销量，参考值不参与保护。设计与验收见 `../specs/2026-11-17-optional-official-sales-design.md` 和 `../../verification-official-sales-reference-20261117.md`。
