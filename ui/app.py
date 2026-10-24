"""SupplyLens Streamlit client. Business rules and credentials stay in the API."""

from __future__ import annotations

import hashlib
import html
import os
from datetime import date
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = 100.0

st.set_page_config(page_title="SupplyLens · 采购到货分析", page_icon="🔎", layout="wide")


class APIError(Exception):
    """A safe, user-facing API failure without transport or credential details."""


def api_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            response = client.request(method, f"{API_URL}{path}", **kwargs)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise APIError("后端返回的数据格式不正确，请查看 API 服务日志。")
        return payload
    except httpx.TimeoutException as exc:
        raise APIError("请求等待超时。请先检查 API 日志，避免立即重复提交同一请求。") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (409, 422):
            try:
                detail = exc.response.json().get("detail")
                if isinstance(detail, dict) and isinstance(detail.get("errors"), list):
                    raise APIError("\n".join(str(error) for error in detail["errors"][:10])) from None
            except (ValueError, TypeError):
                pass
        if status == 404:
            message = "未找到对应记录，可能已在后端重启后失效。请重新发起分析。"
        elif status in (400, 422):
            message = "请求参数未通过校验，请选择一个示例订单后重新提问。"
        else:
            message = f"API 暂时无法完成请求（HTTP {status}），请查看后端日志。"
        raise APIError(message) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise APIError("暂时无法连接到 API。请确认服务已启动，并检查 API_URL 配置。") from exc


def reset_conversation() -> None:
    st.session_state.messages = []
    st.session_state.session_id = None
    if "session_id" in st.query_params:
        del st.query_params["session_id"]


def sync_backend(health: dict[str, Any]) -> None:
    boot_id = str(health.get("boot_id", ""))
    previous = st.session_state.get("boot_id") or st.query_params.get("boot_id")
    if previous and boot_id and previous != boot_id:
        reset_conversation()
        st.session_state.session_notice = "后端已重启，旧对话上下文已失效。已为你开启新会话。"
    if boot_id:
        st.session_state.boot_id = boot_id
        st.query_params["boot_id"] = boot_id


def display_value(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)


def render_sources(report: dict[str, Any]) -> None:
    evidence = report.get("evidence") or []
    if evidence:
        st.markdown("**业务数据证据**")
        for item in evidence:
            label = f"{item.get('id', '—')} · {item.get('description', item.get('source', '数据记录'))}"
            with st.expander(label):
                st.caption(f"来源：{item.get('source', '—')}")
                st.json(item.get("fields", {}))
    else:
        st.caption("本次没有可展示的业务证据。")
    refs = report.get("sop_refs") or []
    if refs:
        st.markdown("**处理规范引用**")
        for item in refs:
            with st.expander(f"{item.get('id', '—')} · {item.get('title', '处理规范')}"):
                st.caption(f"文档：{item.get('document', '—')} · 合成 SOP")
                st.markdown(str(item.get("text", "")))


def render_report(report: dict[str, Any]) -> None:
    facts = report.get("facts") or {}
    st.caption(
        f"订单 {report.get('order_id', '—')} · {facts.get('supplier_name', '—')}"
        f" · 物料 {facts.get('material_code', '—')} · 数据截至 {report.get('as_of', '—')}"
    )
    source = report.get("data_source") or {"kind": "synthetic"}
    st.caption("数据来源：" + ("用户导入 · " + str(source.get("name", "上传文件"))
                              if source.get("kind") == "imported" else "内置模拟数据") + "；SOP 默认为自编模拟流程。")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("订购数量", display_value(facts.get("ordered_qty")))
    c2.metric("已收货数量", display_value(facts.get("received_qty")))
    c3.metric("未收货数量", display_value(facts.get("outstanding_qty")))
    c4.metric("逾期天数", display_value(facts.get("overdue_days")))
    st.caption(f"承诺到货日：{facts.get('promised_date', '—')} · 数量单位：件；未知值不按零处理")

    anomalies = report.get("anomalies") or []
    st.markdown("**订单与批次异常**")
    if anomalies:
        rows = [
            {
                "异常": item.get("title", item.get("code", "—")),
                "批次": item.get("shipment_id") or "订单级",
                "说明": item.get("detail", ""),
                "证据 ID": "、".join(item.get("evidence_ids") or []),
            }
            for item in anomalies
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.info("当前记录未触发到货异常规则。请结合下方信息缺口理解结果。")

    gaps = report.get("data_gaps") or []
    st.markdown("**信息缺口**")
    if gaps:
        for gap in gaps:
            st.warning(str(gap))
    else:
        st.caption("本次分析未记录额外信息缺口。")

    actions = report.get("actions") or []
    if actions:
        st.markdown("**处理建议**")
        for number, action in enumerate(actions, 1):
            st.markdown(f"{number}. {action.get('text', '')}")
            if action.get("sop_ids"):
                st.caption("依据：" + "、".join(action["sop_ids"]))
    if report.get("followup_draft"):
        st.markdown("**供应商跟进建议草稿**")
        st.markdown(str(report["followup_draft"]))
    for note in report.get("notes") or []:
        st.caption(str(note))


def download_template(format: str) -> bytes:
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(API_URL + "/api/imports/template", params={"format": format})
            response.raise_for_status()
            return response.content
    except httpx.HTTPError:
        return b""


def render_import_panel(analysis_date: date) -> None:
    with st.expander("Excel / CSV 数据导入"):
        st.caption("一个 Excel 工作簿，或按表名命名的 CSV。suppliers 和 orders 必填；缺收货表时数量显示未知。")
        for format, label, mime, filename in [
            ("xlsx", "下载 Excel 模板", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "supplylens-import-template.xlsx"),
            ("csv", "下载 CSV 模板包", "application/zip", "supplylens-import-csv.zip"),
        ]:
            content = download_template(format)
            if content:
                st.download_button(label, content, file_name=filename, mime=mime, key="template-" + format)
        uploads = st.file_uploader("选择采购快照文件", type=["xlsx", "csv"], accept_multiple_files=True,
                                   help="每个文件最多 5 MB；CSV 模板 ZIP 请先解压再多选上传。")
        signature = hashlib.sha256((analysis_date.isoformat() + "".join(
            file.name + hashlib.sha256(file.getvalue()).hexdigest() for file in uploads)).encode()).hexdigest()
        if st.session_state.get("import_signature") != signature:
            st.session_state.import_signature = signature
            st.session_state.pop("import_preview", None)
        if st.button("预览并校验", disabled=not uploads):
            try:
                preview = api_request("POST", "/api/imports/preview", data={"as_of": analysis_date.isoformat()},
                                      files=[("files", (file.name, file.getvalue(), file.type or "application/octet-stream")) for file in uploads])
                st.session_state.import_preview = preview
            except APIError as exc:
                st.error(str(exc))
        preview = st.session_state.get("import_preview")
        if preview:
            st.write(f"新增 {preview['new_orders']} 张，更新 {preview['replace_orders']} 张导入订单")
            st.dataframe([{"表": name, "行数": count} for name, count in preview["counts"].items()], hide_index=True)
            st.caption("订单预览（最多五条）")
            st.dataframe(preview["sample_orders"], hide_index=True)
            for warning in preview["warnings"]:
                st.warning(warning)
            confirmed = st.checkbox("确认文件覆盖截至所选日期的数据；同名导入订单将更新，空表代表没有记录，缺表代表未知。",
                                    key="confirm-" + preview["preview_id"])
            if st.button("确认导入", type="primary", disabled=not confirmed):
                try:
                    result = api_request("POST", "/api/imports/commit", json={
                        "preview_id": preview["preview_id"], "confirm_snapshot": confirmed})
                    st.session_state.pop("import_preview", None)
                    reset_conversation()
                    st.session_state.session_notice = f"已导入 {len(result['order_ids'])} 张订单；数据来源会逐单标记。"
                    st.rerun()
                except APIError as exc:
                    st.error(str(exc))


def render_cloud_panel() -> None:
    with st.expander("真实模型 API 联调"):
        try:
            status = api_request("GET", "/api/cloud/status")
        except APIError as exc:
            st.warning(str(exc))
            return
        st.caption(f"聊天模型：{status['model']}；向量模型：{status['embedding_model']}")
        st.caption("密钥状态：" + ("已配置" if status["configured"] else "未配置"))
        if not status["configured"]:
            st.info("请在服务端本地 .env 设置 MODEL_API_KEY，不要把密钥发到聊天或提交 GitHub。")
        if status.get("restart_required"):
            st.warning("本地配置已变化。连接检查会读取新配置；正式分析前请重启 API，并构建对应向量索引。")
        if status["configured"] and status["agent_mode"] != "cloud":
            st.info("当前分析仍是本地规则模式。使用真实模型分析需设置 AGENT_MODE=cloud 和 EMBEDDING_MODE=cloud，构建索引并重启 API。")
        st.caption("检查只发送测试文本，验证向量接口和两轮工具调用，会产生少量模型调用；不会读取订单。")
        if st.button("检查真实 API", disabled=not status["configured"]):
            with st.spinner("正在检查模型接口…"):
                try:
                    st.session_state.cloud_check = api_request("POST", "/api/cloud/check")
                except APIError as exc:
                    st.error(str(exc))
        if result := st.session_state.get("cloud_check"):
            (st.success if result["status"] == "ok" else st.warning)(result["message"])
            st.dataframe(result.get("stages", []), hide_index=True)
            st.caption(f"耗时 {result.get('elapsed_ms', 0):.0f} ms；输入/输出 tokens："
                       f"{display_value(result.get('input_tokens'))} / {display_value(result.get('output_tokens'))}")

def render_steps(steps: list[dict[str, Any]]) -> None:
    if not steps:
        st.caption("本次没有可展示的工具调用记录。")
        return
    for number, step in enumerate(steps, 1):
        duration = step.get("elapsed_ms")
        suffix = f" · {duration:.0f} ms" if isinstance(duration, (float, int)) else ""
        with st.expander(f"{number:02d}  {step.get('name', '工具')} · {step.get('status', '—')}{suffix}"):
            st.caption("调用参数")
            st.json(step.get("arguments") or {})
            st.caption("工具结果")
            st.json(step.get("result") if step.get("result") is not None else {})


def render_response(result: dict[str, Any], position: int) -> None:
    statuses = {"completed": "分析完成", "partial": "部分完成", "needs_input": "需要补充信息", "failed": "分析未完成"}
    status = result.get("status", "")
    if status in ("failed", "partial"):
        st.warning(statuses.get(status, status))
    st.markdown(str(result.get("answer") or "本次没有返回文字回答。"))
    report = result.get("report")
    if report:
        overview, sources, trace = st.tabs(["分析报告", "来源与依据", "工具调用"])
        with overview:
            render_report(report)
        with sources:
            render_sources(report)
        with trace:
            render_steps(result.get("steps") or [])
    elif result.get("steps"):
        with st.expander("查看工具调用"):
            render_steps(result["steps"])

    metrics = result.get("metrics") or {}
    elapsed_ms = metrics.get("elapsed_ms")
    elapsed = f"{elapsed_ms / 1000:.2f} 秒" if isinstance(elapsed_ms, (float, int)) else "—"
    parts = [
        statuses.get(status, status or "—"),
        f"耗时 {elapsed}",
        f"工具调用 {metrics.get('tool_calls', '—')} 次",
    ]
    if result.get("mode") == "cloud":
        parts.append(f"模型轮次 {metrics.get('model_rounds', '—')}")
        input_tokens, output_tokens = metrics.get("input_tokens"), metrics.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            parts.append(f"输入 / 输出 tokens {input_tokens} / {output_tokens}")
    else:
        parts.append("演示工作流 · 未调用语言模型")
    st.caption(" · ".join(parts))
    if result.get("trace_notice"):
        st.caption(str(result["trace_notice"]))
    if result.get("markdown"):
        order_id = (report or {}).get("order_id", "conversation")
        st.download_button(
            "↓ 下载 Markdown 报告",
            data=str(result["markdown"]),
            file_name=f"supplylens-{order_id}-{str(result.get('run_id', 'report'))[:8]}.md",
            mime="text/markdown",
            key=f"download-{position}-{result.get('run_id', '')}",
        )


def main() -> None:
    st.markdown(
        """<style>
        .block-container {max-width: 1200px; padding-top: 2.1rem; padding-bottom: 3rem;}
        .sl-eyebrow {font-size: .76rem; letter-spacing: .16em; color: #64748b; font-weight: 650;}
        .sl-title {font-size: 2.55rem; letter-spacing: -.055em; font-weight: 750; line-height: 1.12; margin: .35rem 0;}
        .sl-subtitle {font-size: .98rem; color: #64748b; margin-bottom: 1rem;}
        .sl-pill {display: inline-block; border: 1px solid #cbd5e1; padding: .22rem .65rem;
          border-radius: 5rem; font-size: .76rem; color: #334155; margin: .12rem .3rem .12rem 0; background: #f8fafc;}
        [data-testid="stMetric"] {background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: .8rem 1rem;}
        [data-testid="stMetricValue"] {font-size: 1.6rem;}
        [data-testid="stChatMessage"] {border: 1px solid #e2e8f0; border-radius: 12px;}
        </style>""",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="sl-eyebrow">PROCUREMENT INTELLIGENCE / 采购到货分析</div>', unsafe_allow_html=True)
    st.markdown('<div class="sl-title">SupplyLens<span style="color:#0d9488">.</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="sl-subtitle">从订单、物流到收货，用可核对的证据解释每一次异常。</div>', unsafe_allow_html=True)

    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("session_id", st.query_params.get("session_id"))
    try:
        health = api_request("GET", "/health")
    except APIError as exc:
        st.error(str(exc))
        st.markdown("在项目根目录启动 API 后，刷新此页：")
        st.code("uv run supplylens init\nuv run supplylens index\nuv run supplylens serve", language="bash")
        st.caption("默认 API_URL 为 http://127.0.0.1:8000；可在 .env 中修改。")
        if st.button("重新连接", type="primary"):
            st.rerun()
        return

    sync_backend(health)
    mode = health.get("agent_mode", "demo")
    embedding = health.get("embedding_mode", "local")
    badges = [
        "演示模式 · 确定性工具工作流" if mode == "demo" else "云端模式 · 模型工具调用",
        "本地词法检索" if embedding == "local" else "云端向量检索",
        f"分析日 {st.session_state.get('analysis_date', health.get('as_of', '—'))}",
    ]
    st.markdown("".join(f'<span class="sl-pill">{html.escape(badge)}</span>' for badge in badges), unsafe_allow_html=True)
    counts = health.get("data_counts", {})
    st.caption(f"内置模拟订单 {counts.get('synthetic', 0)} 张 · 用户导入订单 {counts.get('imported', 0)} 张。来源逐单标记；SOP 默认为自编模拟流程。")
    if st.session_state.get("session_notice"):
        st.info(st.session_state.pop("session_notice"))
    if health.get("database") != "ok":
        st.error("数据库尚未就绪。请检查配置并运行 supplylens init，随后刷新页面。")
        return
    if health.get("status") == "degraded":
        st.warning("部分后端依赖尚未就绪，请查看 API 日志。云端失败不会自动切换为演示结果。")

    prompt: str | None = None
    with st.sidebar:
        st.markdown("### 分析工作台")
        st.caption("选择订单，从一个具体问题开始。")
        try:
            default_date = date.fromisoformat(st.query_params.get("as_of", health.get("as_of", "2026-10-24")))
        except ValueError:
            default_date = date.fromisoformat(health.get("as_of", "2026-10-24"))
        analysis_date = st.date_input("分析截至日期 / 导入快照日期", value=default_date, key="analysis_date")
        if st.query_params.get("as_of") != analysis_date.isoformat():
            st.query_params["as_of"] = analysis_date.isoformat()
        st.caption("按 Asia/Shanghai 当日结束截断；不能将文件覆盖日之后的收货状态视为已知。")
        try:
            orders = api_request("GET", "/api/orders", params={"as_of": analysis_date.isoformat(), "limit": 1000}).get("items") or []
        except APIError as exc:
            orders = []
            st.warning(str(exc))
        choices = {str(item["order_id"]): item for item in orders if isinstance(item, dict) and item.get("order_id")}
        selected = st.selectbox(
            "选择订单",
            list(choices),
            format_func=lambda value: f"{value} · {'导入' if choices[value].get('data_source', {}).get('kind') == 'imported' else '模拟'} · {choices[value].get('supplier_name', '')}",
            placeholder="暂无可选订单",
        )
        if st.button("分析到货情况", type="primary", use_container_width=True, disabled=not selected):
            prompt = f"请分析订单 {selected} 的到货情况，给出异常证据和处理建议。"
        if st.button("生成供应商跟进建议", use_container_width=True, disabled=not selected):
            prompt = f"请分析订单 {selected}，并帮我写一份供应商跟进建议。"
        st.divider()
        render_import_panel(analysis_date)
        render_cloud_panel()
        st.divider()
        st.markdown("**可尝试追问**")
        st.caption("有哪些信息需要补充？\n\n帮我写一份供应商跟进建议")
        if st.button("开启新会话", use_container_width=True):
            reset_conversation()
            st.rerun()
        st.divider()
        st.caption("会话上下文保存在 API 内存中。刷新页面保留会话标识；后端重启后需要重新指定订单。")

    if not st.session_state.messages:
        st.markdown("#### 把一个到货问题，变成一份可追溯的分析")
        st.markdown("在左侧选一个订单，或直接输入订单编号。报告会分别呈现事实、异常、信息缺口和处理依据。")
        a, b, c = st.columns(3)
        with a:
            st.markdown("**01 · 先查事实**")
            st.caption("汇总订单、发运批次、物流事件与收货记录。")
        with b:
            st.markdown("**02 · 再核对异常**")
            st.caption("规则识别逾期、预计到货延迟与未关闭的物流异常。")
        with c:
            st.markdown("**03 · 给出可执行建议**")
            st.caption("引用合成 SOP，说明尚缺哪些信息，保留工具轨迹。")

    for index, entry in enumerate(st.session_state.messages):
        with st.chat_message(entry["role"], avatar="🔎" if entry["role"] == "assistant" else None):
            if entry["role"] == "user":
                st.markdown(entry["content"])
            else:
                render_response(entry["result"], index)

    typed = st.chat_input("输入订单编号与问题，例如：分析这笔订单为什么还没到货", max_chars=2000)
    prompt = typed or prompt
    if not prompt:
        return
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="🔎"):
        with st.spinner("正在核对业务记录与处理依据…"):
            try:
                result = api_request(
                    "POST",
                    "/api/chat",
                    json={"message": prompt, "session_id": st.session_state.get("session_id"), "as_of": analysis_date.isoformat()},
                )
            except APIError as exc:
                st.error(str(exc))
                return
            session_id = result.get("session_id")
            if session_id:
                st.session_state.session_id = session_id
                st.query_params["session_id"] = session_id
            if result.get("session_reset"):
                st.info("旧会话上下文已失效，本次已创建新会话。若未指定订单，请补充订单编号。")
            if result.get("run_id"):
                try:
                    detail = api_request("GET", f"/api/runs/{result['run_id']}")
                    result["steps"] = detail.get("steps") or []
                except APIError:
                    result["trace_notice"] = "分析结果已保留，工具轨迹暂时无法加载。"
        index = len(st.session_state.messages)
        st.session_state.messages.append({"role": "assistant", "result": result})
        render_response(result, index)


if __name__ == "__main__":
    main()
