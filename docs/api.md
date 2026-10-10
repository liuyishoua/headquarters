# 指挥部 API 与操作流程

浏览器 API 使用独立登录会话；所有写请求需要匹配的 Origin、`X-App-Request: 1` 和 JSON 对象。服务配置中的 Channel 地址和凭据文件不向浏览器返回。

| 方法 / 路径 | 用途 |
| --- | --- |
| POST `/api/login`、`/api/logout` | 独立指挥部登录与退出 |
| GET `/api/overview` | 目标、阶段进度、账号、Channel 目录、分工下发状态、近期动作和指挥记录 |
| POST `/api/missions` | 创建目标和有序阶段，初始为草稿 |
| PUT `/api/missions/:id` | 编辑尚未产生执行证据的草稿/暂停目标 |
| POST `/api/missions/:id/assignments` | 为阶段分配已登记账号、agent 和执行策略 |
| DELETE `/api/assignments/:id` | 暂停确认后移除分工；保留历史记录 |
| POST `/api/missions/:id/transition` | `operation`: `start`、`pause`、`advance` |
| POST `/api/pause-all` | 集中暂停所有运行目标，持久生成撤回指令 |
| POST `/api/accounts` | 登记或更新账号共享额度；不会启动监听 |
| POST `/api/missions/:id/outcomes` | 登记人工确认的业务结果，引用成功平台动作回执 |
| POST `/api/actions/:channel/:id/decision` | 审核本指挥部拥有的动作，`approve` 为布尔值 |

创建目标示例：

```json
{
  "name": "商品咨询与客户跟进",
  "objective": "依据商品事实解答咨询，确认客户需求，明确拒绝后停止联系。",
  "knowledge": "只填写可公开用于回复的业务事实。",
  "persona": "简洁自然，不编造承诺。",
  "stages": [
    {
      "id": "consultation",
      "name": "解决咨询",
      "objective": "解决客户的问题。",
      "criterion": {"metric": "outcome", "kind": "resolved", "target": 10}
    }
  ]
}
```

`metric=outcome` 的结果类别：`resolved`、`qualified`、`published`、`confirmed`。`metric=receipt` 的动作类别：`dm.reply`、`comment.reply`、`content.publish`。后者只表示平台确认操作成功。

账号登记需要 `channel_id`、`source_id`、工作台实际使用的 `account_id`、显示名称，以及 `daily_limit`、`min_interval`、`hour_start`、`hour_end`。分工中的 `account_id` 使用登记后返回的指挥部资源 UUID，不是平台账号字符串。

## 下层 Channel 控制协议

使用单独的 Channel `.local/command-key` Bearer 凭据；平台来源凭据、管理台 Cookie 和携带 Origin 的浏览器请求均被拒绝。

| 方法 / 路径 | 用途 |
| --- | --- |
| GET `/control/v1/catalog` | 实际来源、执行器与 agent 目录 |
| PUT `/control/v1/deployments/:goal_id` | 幂等投影执行规则与指挥归属、版本、上下文和短期授权 |
| POST `/control/v1/renewals` | 最多 500 个 `goal_id + revision` 批量续约，返回确认列表 |
| GET `/control/v1/state?after=:cursor` | 每页最多 500 条动作变更；含统计、最近 200 条沟通事项及本地规则冲突目录 |
| POST `/control/v1/actions/:id/decision` | 只审核归属该指挥部的动作；不允许伪造平台执行回执 |

下发字段：`revision`、`mission_id`、`assignment_id`、`context`、`lease_seconds`、`goal`。`goal` 沿用 Channel 单账号执行规则。模型请求的 `context.command` 携带业务目标、阶段、分工和前阶段已核实摘要；原有账号和会话隔离仍生效。

## 首次使用

1. 在 Channel 网络页确认服务连接、平台来源和 agent 目录。
2. 在执行账号页登记工作台稳定账号标识，设置共享额度。
3. 创建目标与阶段，填写业务知识和完成标准。
4. 为阶段分配账号和 agent，默认先审核后执行。
5. 对应平台执行器实际在线且能力匹配后，启动阶段。任何冲突或缺失能力均显示原因。
6. 查看模型方案，审核动作，等待平台回执；核实业务结果后登记证据。
7. 达到标准后验收阶段，确认旧规则停用，再启动下一阶段。
