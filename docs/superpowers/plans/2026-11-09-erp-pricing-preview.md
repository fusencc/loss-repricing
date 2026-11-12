# ERP 采集到只读算价 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution. Steps use checkbox syntax.

**Goal** — 用指定 ERP 四类采集记录和独立补充证据贯通数据准备、补亏计算和管理员对账。
**Architecture** — 适配层按订单和稳定明细 ID 归一化，服务层调用现有领域引擎并保存只读报告。采集覆盖、定价证据、算价判定分别展示。
**Tech Stack** — Python 3.12、Pydantic、FastAPI、Vue 3、现有 Decimal 定价引擎。
**Spec** — `docs/superpowers/specs/2026-11-05-loss-repricing-design.md` 中 2026-11-09 连续接入要求。

## Global Constraints

- 使用当前四类批量采集格式、同一明确指定的采集编号和北京业务日。
- ERP 财务表的 item ID 和 SKU 列表仅为追溯字段。在线键、明细亏损、数量和币种由单独证据提供。
- 金额采用十进制字符串。真实原件留在运行目录。样例使用虚构标识与金额。
- 只读报告独立保存，与执行、通知、试点、回执分开。沿用现有公式和判定。

## Review Focus

- 同单跨组与同商品跨单同时出现时，来源标签齐全、明细计数正确。
- 真实导出中多 SKU 缺少明细毛利时，保留订单或目标问题。
- 无法定位影响目标的缺口、分页或财务覆盖不全时，保持来源未齐备。
- 有可靠目标的缺项只阻断该目标，其他目标继续。
- 旧采集、不同批次的补充文件、未确认的业务日期和合成证据混用均应明确拒绝。

## 任务 1 · 规范化与只读判定

- [x] 在 `tests/test_erp_pricing_preview.py` 用今天采集结构的合成样例复现跨来源重复、多 SKU 和缺亏损。预期 MY 站 A 选 1.20 CNY/件，现价 18.00 MYR，目标实际价 18.72 MYR，含税上传建议 20.80 MYR。
- [x] 执行 `.venv/bin/python -m pytest -q tests/test_erp_pricing_preview.py`，确认还无法生成预期报告。
- [x] 在 `app.adapters.erp_pricing` 实现批次和补充证据合同及数据准备，交由 `app.services.erp_pricing.preview_pricing(capture, evidence)` 调用 `evaluate`。
- [x] 测试全量、部分覆盖、来源状态冲突、独立目标和历史回放。

## 任务 2 · 管理员核对入口

- [x] 增加管理员 API 回归，验证指定编号取源、保存后回读、非法编号及未登录拒绝、执行表无记录。
- [x] 将预览和回读接入 `app.api.erp_rates`，复用 ERP 文件存储路径校验和写后校验。
- [x] 在设置页增加采集号、补充 JSON、覆盖与结果查看，以及报告下载。
- [x] 记录补充 JSON 字段和样例入口。用今天已保存批次核对缺项归因，合成样例验证完整算价。
- [x] 先构建前端，再跑全量测试，并用独立运行目录做网页冒烟。


## 任务 3 · 由已保存预览生成文件对账批次

- [x] 用样例原始导出和补充证据验证管理员选中一个报告后，从 HTTP 入队到 worker、批次和商品判定。
- [x] 同一次保存保留规范快照和已算价判定。批次绑定采集号、报告号及回放时点，同一报告幂等入批。
- [x] 文件模式管理员可显式回放历史日期。对账批次独立于运营队列、协作状态、价格提交及通知。
- [x] 接入工作台回读与查看批次。验证前端构建、完整后端用例及隔离浏览器流程。
