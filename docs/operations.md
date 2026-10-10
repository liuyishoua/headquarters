# 部署维护

正式入口：<https://d.58nft.net:9457/>；回环服务：`127.0.0.1:8781`。

```sh
sudo systemctl status headquarters.service headquarters-proxy.service
curl -fsS https://d.58nft.net:9457/healthz
```

独立数据目录 `.local/`，权限 0700：

- `headquarters.sqlite3`：目标、账号、分工、期望指令、游标、动作副本、业务结果、验收记录和审计。
- `admin-password`、`session-key`：指挥部访问密码与独立会话签名。
- `channels.json`：服务端 Channel 连接配置，格式见 `deploy/channels.example.json`。允许 1–20 个命名连接；远程连接必须使用 HTTPS，不跟随 HTTP 重定向。
- Channel 的 `command-key`：由 Channel 首次启动生成的独立控制凭据，文件 0600。指挥部以文件路径读取，不在网页或公开文档返回内容。

配置新增 Channel 后重启指挥部，不自动注册或启动平台执行器。新 Channel 需要实现 `/control/v1/*` 协议。平台工作台原有来源凭据和登录方式继续独立。

在线数据库备份使用 SQLite backup API，不只复制遗漏 WAL 的主文件。恢复时协调备份两个服务的账本及执行器日志；若单独回滚 Channel 数据库，会使其动作变更游标和指令版本落后，不能直接重置游标、删除账本或盲目重发，需要核对任务和平台执行事实后恢复。

指挥部停止后，Channel 最多在最后一次续约后的 90 秒内还能领取已有授权动作。到期后保留事件和待处理事项，等待恢复授权。已领取的动作可能已经发送，仍需原执行器提交准确回执。指挥部再次启动按原稳定标识和版本恢复下发，不创建另一套重复规则。

Channel 新增 `control_goals`、`control_changes` 表及动作变更触发器，保留原有消息、来源、任务和结果。上线前已在 Channel 私有备份目录保存数据库快照。Channel 原有本地规则未迁移、未自动启用，也没有启动闲鱼可选执行器。指挥部不会重启 Multica agent runtime。

Bifrost 只增加独立入口，主页及模型 API 路由保持现有配置。门户目录每次加载，无需因链接变化重启 Bifrost。
