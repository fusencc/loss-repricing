# 销售系统自动认证验证（2026-11-05）

## 真实只读认证

- 使用用户明确提供的本人销售系统账号。凭据保存本项目忽略的.env，不记入本报告。未读取/复用罗迪或预算程序账号。
- 文档JSON请求返回HTTP400；依据旧客户端已确认form方式请求HTTP200，返回有效access_token、bearer、expires_in、refresh_token与scope。
- 首次有效期179999秒。新增命令 `app.probe_sales_auth --renew-login` 两次登录均成功，该轮有效期179786秒、scope=app；返回有效期递减，未证明换发不同Token，未宣称refresh_token刷新接口已实测。
- 命令仅调用认证接口，不查商品、不导出、不上传文件、不调用priceUpload。调价权限与店铺范围仍未证实；scope=app不能作为证据。LR_LIVE_SUBMIT_ENABLED仍为false。

## 本地代码验证

- 自动获取/复用/到期前重新登录已接入HttpAdapter；仅同一已确认服务源可使用生成的Token。静态Token兼容模式保留；业务POST失败不重传。
- 认证生命周期、短有效期、并发复用、失败不缓存、日志脱敏、来源隔离和POST不重发等11个认证测试通过；认证/HTTP适配/汇率相关59项通过。
- 前端 `npm.cmd run build` 成功；随后项目根目录 `.venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp runtime/<本次独立目录>`：272 passed，1条Starlette/anyio既有弃用警告，117.66秒。
- `git diff --check`无空白错误；扫描Git跟踪及未忽略的文本文件未发现本次业务密码；git check-ignore确认.env被忽略。

## 尚未验证

没有等待实际近50小时到期；到期及失败恢复由可控时钟测试验证。没有验证refresh_token端点、业务调价权限、平台生效或生产Python3.12。没有部署/重启服务；不改变现有其他未提交工作、真实合同、试点和提交开关。
