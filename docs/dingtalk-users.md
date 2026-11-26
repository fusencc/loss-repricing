# 钉钉人员 userID 文件

本项目本机人员目录：`runtime/dingtalk_users.json`。WSL 开发副本的完整路径为 `/home/developer/projects/loss-repricing/runtime/dingtalk_users.json`。

2026-11-13 从桌面 `AIBP公共空间经验库/钉钉userid.txt` 整理，按钉钉 userID 去重后共 66 人；其中 60 人补充了桌面 `钉钉userid.json` 的销售员 `owner_id`。这 60 人的 userID 已与广告预算项目 `backend/data/dingtalk_salespeople.json` 逐项核对一致。来源文件中的重复记录已合并，旧导入辅助字段 `before`、`source` 不带入本项目。

## 字段和调用

顶层 `users` 为人员数组，每人包含：

- `name`：来源姓名或账号备注。部分来源使用手机号作为备注，尚未核实为实名。
- `user_id`：钉钉用户 ID，必须作为字符串处理，保留前导零及原始字符。
- `active`：本地目录启用状态，不表示已实时核验在职或消息可送达。
- `owner_id`：可选，来源销售系统的所属人 ID；60 名销售员有此字段，不能与钉钉 userID 混用，也不能直接认定等于本项目登录账号或店铺负责人 ID。

从项目根目录运行的读取示例：

```python
import json
from pathlib import Path

users = json.loads(
    Path("runtime/dingtalk_users.json").read_text(encoding="utf-8")
)["users"]
by_name = {user["name"]: user["user_id"] for user in users if user["active"]}
recipient_user_id = by_name["常福生"]
```

四部门结果在批次内按每条商品保存的 ERP 销售员姓名分组。`app.services.notifications` 使用本项目 `runtime_dir/dingtalk_users.json` 匹配启用人员，将该人的结果通过机器人私聊发给对应字符串 `user_id`。

姓名仅去除首尾空格后精确匹配，不做模糊匹配；同名不同 ID、缺失 ID、停用人员、同一 ID 对应多个启用姓名时，仅该人的通知失败，其他人继续处理。完全相同的重复人员行不造成歧义。人员目录不可用时保留明确失败原因，不任意选择收件人。

每张业务卡片只含同一部门批次中该销售员的商品。网页账号与商品归属仅控制网页查阅，管理员告警使用独立收件人。

生产 worker 和管理 API 使用同一份机器人配置。`LR_DING_ENABLED=true` 时，worker 自北京时间 08:00 起投递。队列按批次和销售员姓名去重，已受理的消息按当次 User ID 查询回执。

## 维护和运行范围

后续在此文件直接维护本项目人员目录。源文件日期早于整理日期，当前未在线查询通讯录、核实任职或测试送达。2026-11-19 已将用户确认的吴娟商品归属绑定至真实档案并接入机器人私聊，其余名单仍不代表商品归属已确认。运行方式及实际验收见 `docs/dingtalk-notification-runtime.md`。

`runtime/` 已由项目 `.gitignore` 排除，真实名单不会随 Git 提交或克隆同步。其他机器和部署环境需单独放置确认后的名单。文件不包含应用凭据或访问令牌。
