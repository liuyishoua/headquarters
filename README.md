# headquarters

[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)

独立运行的业务目标指挥部。定义目标、阶段和验收标准，再分配 Channel、平台账号和 Multica agent，
持续收集执行回执及经过确认的业务结果。Python / Flask、原生 HTML/JS、SQLite WAL。

## 界面预览

以下为临时演示环境截图，使用虚构业务目标和账号。

![业务目标管理](docs/previews/headquarters-goal.png)

## 本机启动

安装 **Python 3.10+**：

```bash
git clone https://github.com/liuyishoua/headquarters.git
cd headquarters
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m headquarters
```

打开 **http://127.0.0.1:8781**。首次启动生成独立访问密码，保存在 `.local/admin-password`。
初始目标和账号为空；先登记自己的 Channel 连接，再配置目标和分工。按 Ctrl+C 停止。
本项目没有 `--demo` 参数；隔离验收使用测试中的临时存储与模拟连接。

## 连接与使用

连接字段见 [部署说明](docs/operations.md)。机器控制凭据与浏览器登录凭据独立管理。
创建目标不会绕过平台账号权限；只允许实际具备相应能力的在线账号执行。
阶段推进与业务结果验收由用户确认，模型提案、平台送达回执和业务成果分别记录。
暂停目标会撤回下游执行授权；授权过期保留事件并停止领取新动作。

## 数据与备份

- `.local/`：密码、连接配置、业务台账和数据库，全部忽略 Git。
- 指令使用单调版本、短期授权与幂等确认，服务重启后保留业务状态。
- 备份前停止服务并复制整个 `.local/`。

## 安装与验证

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
node --check headquarters/static/app.js
```

测试只使用临时数据库、虚构账号与模拟 Channel，不启动真实平台执行器。

## 目录

- `headquarters/`：业务目标、资源、阶段、指令台账和管理界面。
- `tests/`、`scripts/`：隔离验证工具；`deploy/`：部署示例。
- `docs/`：架构、API、运维及 [完整功能说明](docs/operations-original.md)。

## 使用许可

**本项目仅供学习、研究、测试及其他非商业用途，不得用于商业目的。**
对本仓库中版权所有者拥有权利的代码，采用
[PolyForm Noncommercial License 1.0.0](LICENSE)；商业使用须事先取得版权所有者的单独授权。
本项目属于**源码可见、限制商用**，不应标为 MIT 或 OSI 意义上的开源许可证。

第三方代码、素材和依赖仍受其各自许可条件约束，来源与许可范围见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本声明不追溯改变第三方代码或此前已发布版本的许可。
