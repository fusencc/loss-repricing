# 数据接入样本

`snapshot.demo.json` 是程序自身生成的合成数据，所有DEMO店铺、订单和价格均虚构。它展示Snapshot输入合同，不是公司数据或已验证业务费率。

`docs/snapshot.schema.json` 是完整结构定义。真实取数完成后，在适配器处统一字段，保留源时间、明细标识、数量粒度、币种、费用版本及来源引用。不能把示例verified=true当作真实配置验证通过。

JSON文件日期是生成日。文件对账要求当日数据与汇率；请重新生成快照，而不是修改旧数据日期伪装成新数据。

ERP 只读回放使用 erp-pricing-capture.sample.json 和 erp-pricing-evidence.sample.json。两份均为合成数据，采集夹具采用 2026-11-09 四组财务结果结构。运行方式和字段说明见 docs/erp-pricing-preview.md。
