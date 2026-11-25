# 2026-11-23 提价日志状态说明本地验证

## 修改范围

- `frontend/src/SellerRecordLogs.vue`：独立右侧状态列中，徽标下展示本条原因；店铺行仅保留店铺／站点，不改底层平台字段或下载数据。
- `frontend/src/records-model.ts`：已核验的显示说明固定为「商品符合调价」；待处理、待核验和不调价只读本条保存的 `reason`，缺失或空白显示「原因未记录」。不借历史尝试、销量或价格推导原因。
- `frontend/src/records.css`：说明支持保存的换行及长文本断行，参与自身网格高度；窄屏保持状态独立右列，价格另起一行。
- `frontend/tests/seller-record-logs.test.mjs`：更新状态列和店铺行断言，新增真实保存多原因、缺失原因、固定说明及历史阈值文本转义测试。
- `docs/engineering-standards.md`：补充当前展示合同。

现有 `backend/app/services/records.py::public_record` 已返回 `Item.reason`，无需修改 API、算价、保护或执行逻辑。已阅读确认设计、计算与来源规范和工程规范。用户举例「7天销量>21」与当前七日销量仅作参考的口径不同：本次没有新增此阈值保护，历史已保存文案仍按原样展示，不回写历史。

## 已完成验证

环境：WSL2。本地前端回归 `cd frontend && node --test tests/*.test.mjs`：38 项通过，0 失败，其中日志组件 13 项；包含复制及真实 XLSX 导出回归。

随后执行 `cd frontend && npm run build`：Vue/TypeScript 检查及 Vite 生产构建通过。新主资源为 `index-BIs8S-XX.js`、`index-Dcp-GZOd.css`。

编辑器对 `SellerRecordLogs.vue` 的诊断为零。

## 验证限制

构建完成后，终端工具返回 `Cursor Exec stream closed before returning a terminal result`，因此后续只读 API 回归命令及现有 18827 服务资源哈希请求均未获得执行结果，不计为通过。浏览器服务状态查询显示连接，但导航调用返回 `MCP descriptor not found`；本次尚未完成浏览器实测，需在工具恢复后验证既有 `http://127.0.0.1:18827/` 的新资源、桌面与窄屏布局、复制和 Excel 按钮。未启动其他端口或 worker，未请求任何业务写入，未进行 Git 提交。

上述前端本地测试和构建不代表真实改价、生效核验或生产环境验收。
