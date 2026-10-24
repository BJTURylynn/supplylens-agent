# 验证记录

记录更新日期：**2026-10-25**；离线测试记录日期：**2026-10-24**。业务分析日期：**2026-10-24，Asia/Shanghai**。

## 当前结果

| 验证项 | 状态 | 结果与覆盖范围 |
| --- | --- | --- |
| Python 3.12.14 完整测试 | 通过 | 168 项测试通过，覆盖规则、数据、API、检索、Agent 约束与评测逻辑；见 [JUnit](../evaluation/results/pytest-py312.xml) |
| Python 3.11.16 完整测试 | 通过 | 独立环境下 168 项测试通过；见 [JUnit](../evaluation/results/pytest-py311.xml) |
| Python 3.12.14 固定样例验收 | 通过 | 30/30，含 5 个故障注入案例，真实云模型调用数为 0 |
| Python 3.11.16 固定样例验收 | 通过 | 30/30，含 5 个故障注入案例，真实云模型调用数为 0 |
| Streamlit 模拟接口检查 | 通过 | 订单选择、报告、证据、工具轨迹、下载按钮、多轮追问、后端重启与连接失败提示 |
| 真实 API + 页面检查 | 通过（已列场景） | `PO-1001` 报告、工具轨迹、多轮跟进草稿；`PO-1009` 缺失 ETA 显示黄色信息缺口而非确认整体正常；后端重启后自动清理上下文并提示 |
| Cloud 路径协议测试 | 通过 | 15 项脚本化模型协议测试，覆盖受约束输出与错误处理；另有 1 项真实 `ChatOpenAI` SDK + `httpx.MockTransport` 测试，覆盖完整两轮工具调用与 usage 解析；均未调用真实云 API |
| Excel 上传与实际 API / 页面联调 | 通过 | 上传模板、预览、确认写入、自然语言分析；PO-9001 为 1000 / 600 / 400 件、逾期 2 天；见 [截图](demos/05-imported-report.png) |
| 真实 DashScope 模型与 Embedding | 已尝试，未连通 | 连接探针的向量和聊天阶段均返回 HTTP 401；未发送业务订单，未取得真实模型分析或 Token 用量结果 |
| AIHubMix GLM + 本地检索 | 单例通过 | 2026-10-25 账号额度处理后恢复；官方边缘入口下 PO-1001 完成，工具 3 次、模型 2 轮、37875 ms，输入/输出 5671 / 1583 tokens；见[运行存档](live-model/README.md)。非批量评测 |
| MySQL DDL 编译 | 通过 | 检查表结构能按 MySQL 方言编译，不涉及实际数据库连接 |
| MySQL 运行 | 未运行 | 未连接真实 MySQL 实例执行端到端流程 |
| Docker Compose 部署 | 未运行 | 本机没有 Docker CLI，未启动 `db`、`api`、`ui` 容器 |
| Python 3.13 运行 | 未运行 | 项目声明允许该版本，当前没有运行验证记录 |

15 项脚本化模型协议测试与 1 项真实 SDK／模拟 HTTP 测试都包含在完整的 168 项测试中，不额外累加。SDK 测试使用真正的客户端代码处理模拟服务响应，与脚本化模型测试不同，但同样不代表真实云端效果。最终测试集还覆盖了模糊订单编号清理旧上下文的两个回归案例。真实页面检查与模拟接口检查分别记录：后者可以验证界面分支，但不替代实际 API 联调。

新增导入模块的 33 项测试覆盖 Excel/CSV 一致性、中文字段、数量与关联校验、缺表与空表区分、事务回滚、更新冲突和端到端接口；新增连接检查的 14 项离线测试覆盖真实 SDK 的工具回传、用量解析及认证、限流、超时和协议错误。它们包含在上述 168 项中；这些离线测试不调用真实云 API。

## 评测产物

- [Python 3.12.14 Demo 结果](../evaluation/results/demo.json)
- [Python 3.11.16 Demo 结果](../evaluation/results/demo-py311.json)
- [Python 3.12.14 JUnit 测试报告](../evaluation/results/pytest-py312.xml)
- [Python 3.11.16 JUnit 测试报告](../evaluation/results/pytest-py311.xml)
- [独立验收案例](../evaluation/cases.json)
- [业务规则标准答案](../tests/fixtures/rule_cases.json)

固定案例覆盖正常在途、部分收货、逾期未收、历史物流异常、ETA 延期、ETA 缺失、异常与信息缺口并存，以及未知订单、多轮上下文和故障处理。30/30 表示这 30 个已定义案例符合各自预期，不是开放域准确率，也不是车企生产数据上的效果。

JSON 保留运行时间、Python 版本、模式、模型信息及耗时统计。Demo 使用本地词法检索和确定性工具工作流，`live_cloud_cases` 为 0，tokens 为 `null`；这些值不能用于推断真实模型的成本或响应时间。

## 复现

在 README 的环境初始化完成后执行：

```bash
uv run pytest
uv run ruff check src ui tests
uv run supplylens evaluate --mode demo --output evaluation/results/demo.json
uv run supplylens demo --output docs/demos
```

最后一个命令重新生成三段 Demo 的 JSON 和 Markdown。PNG 采集自实际页面，GIF 由这些截图逐帧组装，并非连续实时录屏；原始 PNG 随仓库保留。图片和动图不由该命令生成。

配置有效模型密钥后再运行：

```bash
uv run supplylens evaluate --mode cloud --output evaluation/results/cloud.json
```

保留新结果的模型、模式、时间和失败案例，并更新本表。真实云端调用失败应如实记录，不能用本地 Demo 结果补齐。

## 报告约束验证的含义

业务事实来自确定性工具；处理建议只允许选择本次检索 SOP 的完整原文行；模型备注只允许预定义模板；跟进草稿由程序生成。测试验证这些结构与原文归属约束，以及违规输出的拒绝和修正流程。它们不证明任意自由生成建议已被语义核实，也不替代实际业务对 SOP 适用性的审核。
