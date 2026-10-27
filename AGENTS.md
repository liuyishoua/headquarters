# Headquarters

Independent business control plane. Own objectives, stages, resource allocation,
business outcomes and desired deployments. Multica owns native planning and
project/task records; social workbenches own business tasks and platform side
effects. Channel owns the workbench model-event/result chain and its audit.

- Keep the SQLite ledger and durable desired state independent of Channel.
- Never treat a model proposal as a completed business outcome. Count platform
  delivery only from authenticated workbench business results (legacy in-flight
  Channel receipts may be reconciled); business outcomes need
  human confirmation and evidence tied to the same mission/stage.
- Cross-platform identity must be explicitly recorded, never inferred by nickname.
- Credentials live in ignored `.local/`, never return them to the browser.
- Source credentials and browser sessions cannot access Channel control APIs.
- Social write acceptance uses temporary stores and synthetic accounts. Explicitly
  authorized owned-account reads may be verified through normal authenticated APIs.
  Do not activate real platform connectors or send real messages while testing.
- Run `.venv/bin/python -m unittest discover -s tests -v`, Channel regression,
  JavaScript syntax checks and isolated desktop/mobile browser acceptance.

- 指挥中心原生主单/行动/结果是推进记录：先关闭自身 child_done 唤醒，再以 suppress_run 的独立幂等 PUT 指定已保存规划 Agent；所有 issue 状态同步都抑制再次运行。实际模型子任务保留 Channel 的真实执行绑定。不得直接在镜像 POST 填 assignee 触发双调度。回归见 tests/test_native_responsibility.py，说明见 docs/native-agent-responsibility.md。
