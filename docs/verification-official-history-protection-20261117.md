# 2026-11-17 近三天调价保护改为开放接口

## 当前规则
用户明确授权替换原本程序生效历史来源。真实模式使用POST /Api/tiktok-schedule-server/aibp/getOperationLogCountBySellerSkuIdsAndTypeAndCompleteTime，sellerSkuIds为具体子商品ID，type=2，按北京时间查询时刻向前滚动72小时的完成时间范围。

- 明确count>0：本轮不调；明确count=0：通过近期调价筛选。
- 缺行、重复行、无效数量、HTTP/业务错误：该目标待处理，独立有效目标继续。
- 覆盖ERP准备、真实服务刷新和自动提交前单SKU复查；无候选时不发日志查询。
- 本地档案用于人工保护与未完成执行防重；真实模式移除本地生效时间冷却回退。
- 已有明确授权人工修正继续跳过自动近期调价筛选，保留历史可确认与未完成任务锁要求。
- 保存计数、窗口、来源编号及响应哈希，不伪造上次生效时间。

接口未确认只统计成功记录，也未独立验收完成时间起止秒的包含性；当前按官方返回的type=2调价日志判断，不能把计数当作成功生效证明。demo/file仍为独立模拟和文件对账输入。

## 验证

- WSL frontend：npm run build 通过。
- WSL全量：.venv/bin/python -m pytest -q --basetemp=/dev/shm/loss-repricing-official-history-20261117-verified，821 passed，1条既有Starlette/AnyIO弃用警告，26.05秒。临时测试数据库和文件放在tmpfs；此前普通临时磁盘全量中旧历史测试输入失败，更新后相关62项通过，最终全量为零失败。
- git diff --check通过。
- 独立只读代码审查未发现重要/严重问题；已补充分组失败隔离、来源环境校验与人工修正保护回归。
- 真实只读：Tiktok-22070PH / HHX1983-A1 / 子商品1732512893083616534，在2026-11-14 17:18:26至2026-11-17 17:18:26北京时间窗口返回count=3；新规则对应本轮不调。原件保存在runtime/official-price-history/official-recent-price-2a76b0733e354d85a48f43466e5d2178.json。

本次完成代码修改及验证；没有执行部署、重启或真实价格上传。
