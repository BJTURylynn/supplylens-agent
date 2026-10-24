# 真实模型 API 联调

联调与正式订单分析分开进行。连接检查只发送固定测试文本，不读取订单；成功表示当前启用的云端接口检查完成，不代表所有业务问答都已评测通过。本地检索模式会跳过向量接口。

## AIHubMix 免费 GLM + 本地检索

该组合已用模板示例订单实际跑通。按[模型官方页面](https://aihubmix.com/model/coding-glm-5.3-flash-free)配置本地 `.env`：

```dotenv
MODEL_BASE_URL=https://aihubmix.com/v1
MODEL_NAME=coding-glm-5.3-flash-free
MODEL_EXTRA_BODY={}
AGENT_MODE=cloud
EMBEDDING_MODE=local
MODEL_TIMEOUT_SECONDS=40
REQUEST_TIMEOUT_SECONDS=60
```

另填自己的 `MODEL_API_KEY`，不要提交。`MODEL_EXTRA_BODY={}` 用于去掉默认千问专用参数；真实语言模型负责工具调用，本地词法检索负责 SOP，不请求云端 Embedding。修改后重启 API。若用 Docker Compose，需在 `api.environment` 中同时传入 `MODEL_EXTRA_BODY`（例如 `MODEL_EXTRA_BODY: '{}'`）。

2026-10-24 实测：连接探针两轮调用成功；导入模板订单 PO-9001 的完整分析完成，工具调用 3 次、模型交互 2 轮，耗时 35188 ms，服务返回输入 5189 / 输出 1420 tokens。报告为订购 1000、已收 600、未收 400 件，逾期 2 天。这是单例联调，不是批量真实模型评测；不代表其他账号、时间或数据都能达到相同结果。

随后页面复测遇到平台的未充值账号 10 次试用限制；平台以 HTTP 200 聊天文本返回额度提示。项目会识别该提示、停止重试并显示试用额度已用完，不把它当作模型业务分析结果。免费模型仍可能受账号试用门槛和配额限制；需在平台控制台核对。项目不会自动充值或换用收费模型。

## 本地配置

把 `.env.example` 复制为本地 `.env`，填写 `MODEL_API_KEY`。密钥保持在本地，不要发到聊天或提交 GitHub。默认接入阿里云百炼：

```dotenv
MODEL_API_KEY=填写你自己的密钥
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-plus
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
```

API 地址、密钥和模型权限需要匹配服务区域；如果控制台提供专属 Workspace 地址，使用控制台给出的地址覆盖 `MODEL_BASE_URL`。参考[官方密钥说明](https://help.aliyun.com/zh/model-studio/get-api-key)、[工具调用说明](https://help.aliyun.com/zh/model-studio/qwen-function-calling)、[向量接口说明](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。

## 第一步：检查连接

在页面侧栏展开“真实模型 API 联调”，点击“检查真实 API”，或者运行：

```bash
uv run supplylens check-cloud
```

检查包括一次探针工具调用，以及回传工具结果后的模型确认；仅在 `EMBEDDING_MODE=cloud` 时增加向量请求。本地检索显示向量阶段 `skipped`，不代表云端向量已连通。结果保存在本地被忽略的 `runtime/cloud-check.json`，只记录状态、阶段、耗时和服务实际返回的 Token 计数，不保存密钥或模型回复原文。

未配置密钥时返回 `not_configured`，不会假装成功或切换为模拟服务。检查失败会区分认证/区域/权限、限流或额度、超时、网络和响应协议问题。

页面连接检查可重新读取刚保存的 `.env`。正式分析仍使用 API 启动时的配置；配置发生变化时，页面会提示重启。

## 第二步：启用真实模型分析

在 `.env` 设置：

```dotenv
AGENT_MODE=cloud
EMBEDDING_MODE=cloud
```

重新构建索引并重启 API：

```bash
uv run supplylens index
uv run supplylens serve
```

先使用模板中的示例订单完成“文件导入 → 分析 → 查看工具调用 → 下载报告”，再使用实际业务文件。Cloud 分析会把当前订单的相关记录和 SOP 片段发给已配置模型服务；UI 顶部会显示云端模式。

业务数量和异常仍由代码计算，模型选择工具及流程原文建议。密钥、数据库连接密码不会进入模型上下文或报告。只有实际服务返回了用量，才显示 Token 数；不使用估算数字冒充计费结果。

## 评测边界

仓库包含真实 SDK 配合模拟 HTTP 的连接协议测试，验证成功、失败、工具回传和脱敏处理。这些是离线测试。真实联调必须使用自己的有效密钥实际运行，`check-cloud` 中启用的阶段必须全部成功；本地检索的向量阶段应明确跳过。

2026-10-24 实际探针记录：向量与聊天阶段均返回 HTTP 401，尚未连通。未发送业务订单；需修正本地认证配置后重试。此记录不代表模型分析能力评测。

