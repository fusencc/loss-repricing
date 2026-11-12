# 本人账号批量抓取核验（2026-11-09）

## 来源选择
推荐当前已验证的ERP批量亏损池→按订单号导出完整订单财务路线。
8585参考脚本明确为昨日发货SKU粒度，未证明覆盖未发货亏损池；不是仅替换账号即可迁移。
正式/open目前没有已确认替代合同；不宣称网页后端获得正式长期支持承诺。

## 浏览器证据
本人常福生已登录；筛选Tiktok/集团二部/可配货/无日期。
列表POST /Api/deficit/list，参数status=4/platform=Tiktok/bloc=集团二部/limit=10/page=1，4997条。
下载POST /Api/deficit/dowDeficitOrder，参数status=4/platform=Tiktok/bloc=集团二部；HTTP200，application/vnd.ms-excel。
未读取或复制浏览器凭据；未使用罗迪账号。

## 项目真实采集
采集编号 erp-capture-20261109T034515662227Z-54694febd4824e20b87c1e055e4d7db0。
亏损池5011条，选前20笔财务返回20笔，缺失0、状态差异0，全部可配货，未限流。
原始文件2份SHA256复核通过；selection_complete=true，sample_only=true，capture_complete=false。
与网页不同查询时点的4997不构成固定快照集合一致证明。
费用及毛利来自ERP导出，未复算、不计算提价资格、不提交调价。

## 运行验证
桌面Windows独立.venv已安装httpx0.28.1/openpyxl3.1.5；--check通过。
独立文件在无仓库导入条件下通过成功、错单、平台不符、超时、限流、状态变化和空状态回归。
项目+桌面相关32项回归通过（1条既有Starlette/AnyIO弃用警告）；前端构建通过。
Windows独立文件真实运行通过：本人常福生账号，亏损池5019条，选20笔返回20、缺失0、状态差异0、未限流。
原始两份XLSX SHA256复核通过，20笔业务白名单字段与JSON逐行完全一致。实际输出位于桌面ERP亏损数据抓取/验证输出_20261109。
桌面程序没有内置凭据；本次测试经本机内存管道使用已有项目本人配置，不保存密码或浏览器令牌。
全量后端回归356项通过（237.87秒），1条既有Starlette/AnyIO弃用警告；前端构建通过，git diff --check通过。
