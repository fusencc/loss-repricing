# Thai VAT Source Implementation Plan

> For agentic workers: use superpowers:executing-plans for inline execution.

**Goal:** 接入泰国在线类目VAT，新增VAT请求为0。
**Architecture:** fixed_pricing_fees选择父商品税率，live_online_preparation记录来源，费用公式保持不变。
**Tech Stack:** Python 3.12 / Decimal / pytest。
**Spec:** ../specs/2026-11-16-th-vat-source-design.md

## Constraints and review focus

官方优先；受字段缺口阻塞后使用现有已采集字段。不部署不提交价格。0有效、缺值不回退、佣金精确匹配、海外及非TH不变、来源可追溯。

## Task

- [x] 核对官方文档和真实响应。
- [x] 写选率和准备阶段回归测试，确认失败：9项预期失败，30项通过。
- [x] 实现TH在线VAT，移除V列读取，记录来源；初步39项通过。
- [x] 运行相关测试、更新当前规范、检查diff。

## Rulings

旧固定配置原件可保留V列历史数据，当前代码不消费；不增加兼容分支。当前工作区已有大量在用变更，本轮保留全部，仅作相关局部修改。

## Validation

相关10组测试211项通过（34.10秒），覆盖固定费率、在线准备、费用模型、在线利润、用户规则、采集、平台来源、公式合同、定价及ERP编排。未部署、未上传价格；官方三条查询本轮均成功但无VAT字段。

独立只读复查未发现本轮范围内需要修复的实质问题；git diff --check通过。
