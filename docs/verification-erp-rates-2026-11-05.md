# 公司汇率只读接入验证（2026-11-05）

- 本地 Python 3.13.15，前端 `npm.cmd run build` 成功（含 vue-tsc）。
- 根目录 `.venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp runtime/<本次独立测试目录>`：257 passed，1条既有 Starlette/anyio 弃用警告；114.20秒。
- 新增 `tests/test_erp_rates.py`：9 passed。覆盖登录两种token字段、认证失败、只读GET、Decimal转换、逐币种异常与重复币种隔离、不伪造生效日期、业务失败/网络异常脱敏、缺凭据不请求及API管理员权限。
- 默认pytest临时目录首次运行受本机访问权限限制；改用新建项目runtime测试目录后全量通过；禁用无权限的默认缓存。未修改系统临时目录权限。
- 使用本次新增 `query_erp_rates(Settings())` 真实只读查询：北京时间2026-11-05 15:05:46返回56条，五个站点币种均无转换错误。只输出摘要，未持久化上游原始响应或凭据。该证据只证明当前配置下登录/查询可用。
- `git diff --check` 无空白错误。现有未提交工作保留，未提交Git、未部署或重启生产服务。
- 未做浏览器点击/视觉验收、生产Python 3.12验证；汇率生效日期及公司核算方向未确认，结果未注入自动调价Snapshot，未执行真实改价。
