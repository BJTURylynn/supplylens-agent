import re
from typing import Any

from supplylens.schemas import Action, Narrative, Report

SAFE_NOTES = (
    "原因与责任归属需要业务记录或人工核实，流程文档仅用于处理建议。",
    "当前结论仅覆盖本次分析日期内可见的数据快照。",
    "存在信息缺口时，不能据此确认整体正常。",
    "处理建议选自本次检索的流程原文，需人工核实后执行。",
)


def source_lines(ref: dict[str, Any]) -> list[str]:
    """Return complete source sentences eligible for attributed extraction."""
    result = []
    for line in ref["text"].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cleaned = re.sub(r"^\s*(?:[-*]\s+|\d+[.)、]\s*)", "", line).strip()
        if cleaned and len(cleaned) <= 500 and "模拟" not in cleaned:
            result.append(cleaned)
    return result


def validate_narrative(narrative: Narrative, sop_refs: list[dict[str, Any]]) -> None:
    """Validate extractive recommendations, not merely the existence of a citation."""
    by_id = {r["id"]: r for r in sop_refs}
    for action in narrative.actions:
        if not action.sop_ids or not set(action.sop_ids).issubset(by_id):
            raise ValueError("处理建议必须引用本次实际检索到的 SOP 段落")
        if any(action.text not in source_lines(by_id[rid]) for rid in action.sop_ids):
            raise ValueError("处理建议必须逐字选自所引用段落的完整原文行")
    if any(note not in SAFE_NOTES for note in narrative.notes):
        raise ValueError("说明只能选择预定义的证据边界模板，不允许新增定性业务事实")


def default_narrative(sop_refs: list[dict[str, Any]]) -> Narrative:
    actions = []
    for ref in sop_refs[:3]:
        candidates = source_lines(ref)
        if candidates:
            actions.append(Action(text=candidates[-1], sop_ids=[ref["id"]]))
    narrative = Narrative(actions=actions, notes=[SAFE_NOTES[0], SAFE_NOTES[3]])
    validate_narrative(narrative, sop_refs)
    return narrative


def build_report(analysis: dict, refs: list[dict], narrative: Narrative, followup: bool) -> Report:
    report = Report(
        order_id=analysis["order_id"], as_of=analysis["as_of"], facts=analysis["facts"],
        anomalies=analysis["anomalies"], data_gaps=analysis["data_gaps"],
        evidence=analysis["evidence"], sop_refs=refs, actions=narrative.actions, notes=narrative.notes,
        data_source=analysis.get("data_source", {"kind": "synthetic", "name": "内置模拟数据"}),
    )
    for note in (SAFE_NOTES[0], SAFE_NOTES[3]):
        if note not in report.notes:
            report.notes.append(note)
    if not refs:
        report.notes.append("未检索到相关流程文档；业务事实仍有效，处理规则需要人工核实。")
    if followup:
        f = report.facts
        if f["outstanding_qty"] is None:
            requests = "当前收货快照不完整，已收与未收数量未知。请提供截至分析日的完整收货记录后再核对交付情况。"
        elif f["outstanding_qty"]:
            requests = (
                "请协助确认：\n"
                "- 未收齐批次的运单、当前运输状态及对应凭证；\n"
                "- 剩余数量的最新预计到货日期与恢复计划；\n"
                "- 本次核实的回复时间和后续负责人。"
            )
        else:
            requests = "本单已收齐，请核对收货数量与凭证是否一致；如无差异可按流程归档，无需催交。"
        report.followup_draft = (
            f"主题：采购订单 {report.order_id} 到货情况核实（草稿）\n\n"
            f"{f['supplier_name']}，您好：\n\n截至 {report.as_of}，采购单 {report.order_id} "
            f"（{f['material_code']}）采购数量 {f['ordered_qty']} 件，已收 {f['received_qty'] if f['received_qty'] is not None else '未知'}，"
            f"未收 {f['outstanding_qty'] if f['outstanding_qty'] is not None else '未知'}，承诺到货日为 {f['promised_date']}。\n\n"
            f"{requests}\n\n如记录存在差异，请提供相应运单或签收凭证，以便进一步核对。\n\n"
            "本草稿仅供人工审核，尚未发送。数据来源请参见本报告的来源标识。"
        )
    return report


def _escape(value: Any) -> str:
    if value is None:
        return "未知"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report, mode: str) -> str:
    f = report.facts
    lines = [
        f"# {report.order_id} 采购到货分析", "",
        f"> **{'用户导入数据' if report.data_source.get('kind') == 'imported' else '模拟数据'} / 自编模拟流程** · 截至 {report.as_of} 当日结束（Asia/Shanghai）",
        f"> 数据来源：{_escape(report.data_source.get('name', '内置模拟数据'))}",
        f"> 运行模式：{'无模型规则演示（非云端模型评测）' if mode == 'demo' else '云端模型工具调用'}", "",
        "## 订单概况", "", "| 项目 | 数值 |", "|---|---|",
    ]
    labels = {"supplier_name": "供应商", "material_code": "物料", "ordered_qty": "采购数量",
              "received_qty": "已收数量", "outstanding_qty": "未收数量",
              "promised_date": "承诺到货日", "overdue_days": "逾期天数"}
    lines += [f"| {label} | {_escape(f[key])} |" for key, label in labels.items()]
    lines += ["", "## 已确认异常", ""]
    if report.anomalies:
        for anomaly in report.anomalies:
            refs = "、".join(f"[{i}]" for i in anomaly.get("evidence_ids", []))
            lines.append(f"- **{anomaly['title']}**：{anomaly['detail']} {refs}")
    elif report.data_gaps:
        lines.append("当前已知记录未命中异常规则；存在信息缺口，不能据此确认整体正常。")
    else:
        lines.append("当前快照未命中本项目定义的到货异常规则。")
    lines += ["", "## 待核实信息", ""]
    lines += [f"- {gap}" for gap in report.data_gaps] or ["- 当前快照无已识别的关键数据缺口。"]
    lines += ["", "## 处理建议", ""]
    lines += [f"- {a.text} {' '.join('[' + s + ']' for s in a.sop_ids)}" for a in report.actions] or [
        "- 暂无经过引用校验的处理建议，请人工核对业务流程。"]
    lines += ["", "## 数据证据", ""]
    for evidence in report.evidence:
        lines.append(f"- **[{evidence['id']}]** {_escape(evidence.get('description', ''))} "
                     f"— `{evidence.get('source', 'business')}`：{_escape(evidence.get('fields', {}))}")
    lines += ["", "## 流程引用", ""]
    for ref in report.sop_refs:
        lines.extend([f"### [{ref['id']}] {ref['title']}", f"来源：{ref['document']}", "", ref["text"], ""])
    lines += ["", "## 说明", ""] + [f"- {note}" for note in report.notes]
    if report.followup_draft:
        lines += ["", "## 供应商跟进草稿", "", report.followup_draft]
    return "\n".join(lines) + "\n"
