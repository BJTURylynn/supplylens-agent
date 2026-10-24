# 实际执行与页面演示

本目录的 JSON、Markdown 来自 `supplylens demo` 的真实本地工作流执行；页面 PNG 通过浏览器访问实际 FastAPI + Streamlit 服务取得。GIF 是这些实际 PNG 的逐帧组装，**不是连续实时录屏，也未使用 AI 绘图生成界面**。

所有案例使用模拟数据与模拟 SOP；演示模式不调用云端聊天或语义向量模型。

| 演示 | 报告 | 页面动图 |
|---|---|---|
| 逾期部分收货与物流滞留 | [Markdown](01-overdue.md) / [JSON](01-overdue.json) | [GIF](01-overdue.gif) |
| 多轮跟进与草稿 | [Markdown](02-followup.md) / [JSON](02-followup.json) | [GIF](02-followup.gif) |
| 缺少 ETA 的信息缺口 | [Markdown](03-missing-data.md) / [JSON](03-missing-data.json) | [GIF](03-missing-data.gif) |

重新运行示例：`uv run supplylens demo`。在保留原始 PNG 的情况下重建动图：`uv run python scripts/create_demo_gifs.py`。

