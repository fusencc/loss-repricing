# ERP只读取数诊断本地验证（2026-11-05）

## 变更范围

- 新增 `backend/app/adapters/erp_validation.py`，将已有四组第一页采集结果整理为只读诊断报告。
- `erp_capture.py`保留已确认请求及字段白名单，增加脱敏HTTP/业务错误证据，异常total标未知，空白订单标识不发明细请求。
- 新增管理员 `GET /api/sources/erp/validation?limit=2`，仅返回报告；旧sample接口附加报告，原字段仍保留。
- 新增运行配置中的ERP验证面板，支持主动查询、覆盖情况、样本订单/店铺标识、明细问题和报告下载；不预取、不轮询、不自动重试。
- 命令行新增 `--report-only`，仅在受Git忽略的runtime目录保存报告，不保存原始明细。
- 更新来源说明、计算规范的接入边界、工程规范及README入口。

本项目业务公式、费用参数、合同确认、试点名单与真实提交开关均未修改。
报告固定抽样、未完整、未可算价及权限范围未验收；总数相等或返回零条不构成权限完整证据。
诊断不会写Batch/Item/Execution，也不会进入调价计算或上传路径。

## 本次实际验证

环境：Windows，本项目`.venv`的Python及现有前端依赖；未进行生产环境验收。
先执行前端构建，再执行后端测试。

1. 在 `frontend` 执行 `npm.cmd run build`：通过Vue/TypeScript检查及Vite构建。
2. 在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=D:\projects\loss-repricing\runtime\validation-final-pytest-20261105 --tb=short
```

结果：302 passed，1条Starlette/AnyIO兼容别名弃用警告。
采用独立临时目录规避本机默认pytest临时目录访问权限问题。

3. 执行 `.\.venv\Scripts\python.exe deploy\smoke-source-ui.py`：通过。
   使用临时demo数据库和合成报告，`_env_file=None`，不启动worker或真实来源请求。
   核对打开页面不触发采集、点击后只调用一次、部分403与第一页提示、下载JSON、390像素窄屏无整页横向溢出、
   新请求失败时清除旧报告且不提供旧下载。截图和合成下载保存在`runtime/source-ui-check`，不入Git。
4. `git diff --check`：通过；仅提示已有文件工作区CRLF后续转LF，无空白错误。

新增回归覆盖：四来源名称、只描述观察范围、总数未知/矛盾/零条、原始数量/币种/SKU问题、
明细失败与缺订单标识区分、HTTP拒绝与业务/网络失败区分、报告不增加请求、敏感响应不回传、
管理员鉴权、无执行记录副作用、命令行report-only不保存原始成本明细。

## 未验证及下一步

- 本次没有调用真实ERP/销售/钉钉接口，没有真实价格上传或实际送达验证。
- 未完成全量分页、人工导出自动比对、授权店铺范围确认或多来源在线快照整理。
- 未确认真实亏损字段/币种/数量粒度、多SKU分摊、公司正常一次毛利公式、费用价格区间或当日汇率生效日期。
- 未完成生产Python 3.12/MySQL/容器或真实试点验收。
- 下一轮由管理员主动运行只读诊断，使用同账号同筛选人工导出和批准店铺清单对账；
  确认字段语义与公司公式后，才整理可执行Snapshot。当前诊断通过不能代替这些证据。
