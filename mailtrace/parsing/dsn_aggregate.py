"""多封退信回执（DSN）的逐收件人投递状态归并，仅依赖 Python 标准库。

设计要点：
- 输入是**完整原始 DSN 邮件**的数组（页面粘贴用
  :func:`mailtrace.parsing.batch.split_batch_headers` 按同一套
  “===== 下一封邮件 =====”分隔行切分），逐封复用
  :func:`mailtrace.parsing.dsn.inspect_dsn` 的逐收件人判读，
  不重复实现 MIME / 状态码解析。
- **投递键**：对每个收件人，以其所属投递状态部分（``message/
  delivery-status``）中**唯一且非空**的 ``Original-Envelope-Id``
  与 ``Final-Recipient`` 的**原值**（折行展开后的完整原文，含
  ``rfc822;`` 等类型前缀）共同组成。两者缺一不可；Original-
  Envelope-Id 缺失 / 为空 / 在该部分内重复，或该收件人本身被判为
  **待核对**（``review``）时，该收件人列入**未归并清单**并逐条
  说明原因——绝不只凭收件地址合并（同一地址在不同原信投递中可能
  重复出现）。
- 每组按收件人在输入中的**出现顺序**组成状态轨迹：暂缓
  （``4.x.x``）之后可由成功（``2.x.x``）或永久失败（``5.x.x``）
  **结案**；已经结案后再出现**不同类别**的事件则标记为**冲突**，
  结案结论不被静默覆盖；同类别事件重复出现不算冲突。只有暂缓事件
  的组保持“暂缓中（未结案）”。
- 某封邮件没有投递状态部分（或无法按 MIME 解析）时，该封单独记入
  ``message_errors``，其余邮件仍正常参与归并。
"""

from __future__ import annotations

from mailtrace.parsing.dsn import (
    OEID_DUPLICATE,
    OEID_EMPTY,
    OEID_MISSING,
    OEID_OK,
    RESULT_DEFERRED,
    RESULT_PERMANENT,
    RESULT_REVIEW,
    RESULT_SUCCESS,
    _STATUS_LABELS,
    inspect_dsn,
)

# 归并组最终状态：在 success / deferred / permanent / review 之外，
# 结案后出现不同类别事件时为 conflict
FINAL_CONFLICT = "conflict"

_FINAL_LABELS = {
    RESULT_SUCCESS: "成功（投递成功）",
    RESULT_DEFERRED: "暂缓中（仍可能重试）",
    RESULT_PERMANENT: "永久失败（退信）",
    FINAL_CONFLICT: "冲突（结案后出现不同类别状态）",
}

# 可结案的类别：暂缓之后出现其一即结案，结案类别即最终状态
_TERMINAL_RESULTS = (RESULT_SUCCESS, RESULT_PERMANENT)

_OEID_REASON_LABELS = {
    OEID_MISSING: "所属投递状态部分缺少 Original-Envelope-Id 字段"
                  "（RFC 3464 每消息字段），无法可靠标识原信投递，"
                  "不凭收件地址单独合并",
    OEID_EMPTY: "所属投递状态部分的 Original-Envelope-Id 字段为空，"
                "无法可靠标识原信投递，不凭收件地址单独合并",
    OEID_DUPLICATE: "Original-Envelope-Id 在所属投递状态部分中不唯一"
                    "（出现 {count} 次，RFC 3464 规定每部分至多一条），"
                    "无法确定唯一原信投递，该部分各收件人均不予归并",
}


def _event_view(recipient: dict, mail_index: int, event_index: int) -> dict:
    """逐次证据：复用 inspect_dsn 的逐收件人判读结果，只追加定位信息。"""
    event = dict(recipient)
    event["mail_index"] = mail_index
    event["event_index"] = event_index
    return event


def _make_group(key: tuple[str, str], first_event: dict) -> dict:
    """以首个事件初始化一个归并组（key = (信封标识, Final-Recipient 原值)）。"""
    result = first_event["result"]
    closed = result in _TERMINAL_RESULTS
    return {
        "index": 0,  # 由调用方按首见顺序统一编号
        "original_envelope_id": key[0],
        "final_recipient_raw": key[1],
        "final_recipient": first_event.get("final_recipient"),
        "recipient_type": first_event.get("recipient_type"),
        "final_result": result,
        "final_result_label": _FINAL_LABELS[result],
        "closed": closed,
        # 首次结案的类别（冲突说明引用，不随后续事件改变）
        "close_result": result if closed else None,
        "conflict": False,
        "conflicts": [],
        "event_count": 1,
        "mail_indexes": [first_event["mail_index"]],
        "first_mail_index": first_event["mail_index"],
        "events": [first_event],
    }


def _append_event(group: dict, event: dict) -> None:
    """把一次收件人判读按时间顺序追加进组，维护结案 / 冲突状态。

    状态机：
    - 暂缓 → 成功 / 永久失败：结案（最终状态取结案类别）；
    - 暂缓 → 暂缓：继续暂缓；
    - 已结案 → 同结案类别：重复回执，不算冲突；
    - 已结案 → 不同的结案类别（成功 ↔ 永久失败）：标记冲突，
      原结案结论保留，不被覆盖；
    - 已结案 → 暂缓：标记冲突（结案后不应再出现暂缓）。
    """
    result = event["result"]
    previous = group["events"][-1]["result"]
    group["events"].append(event)
    group["event_count"] += 1
    if event["mail_index"] not in group["mail_indexes"]:
        group["mail_indexes"].append(event["mail_index"])

    if not group["closed"]:
        # 尚未结案：只有暂缓能走到这里（首事件为成功 / 永久失败时
        # 建组即结案）。暂缓被结案类别结案。
        if result in _TERMINAL_RESULTS:
            group["closed"] = True
            group["final_result"] = result
            group["final_result_label"] = _FINAL_LABELS[result]
        return

    if result == RESULT_DEFERRED:
        group["conflict"] = True
        group["final_result"] = FINAL_CONFLICT
        group["final_result_label"] = _FINAL_LABELS[FINAL_CONFLICT]
        group["conflicts"].append(
            f"第 {event['mail_index']} 封在已结案（{group['events'][-2]['result_label']}）"
            f"后又出现暂缓事件（前一事件：{previous}），结案结论不覆盖，标记为冲突"
        )
        return

    if result in _TERMINAL_RESULTS and result != group["final_result"]:
        # 以建组结案类别为准记录冲突（此时 final_result 仍是首个结案类别）
        closed_label = _STATUS_LABELS.get(group["final_result"], group["final_result"])
        event_label = _STATUS_LABELS.get(result, result)
        group["conflict"] = True
        group["final_result"] = FINAL_CONFLICT
        group["final_result_label"] = _FINAL_LABELS[FINAL_CONFLICT]
        group["conflicts"].append(
            f"第 {event['mail_index']} 封在已按“{closed_label}”结案后又出现"
            f"“{event_label}”（不同类别），结案结论不静默覆盖，标记为冲突"
        )


def aggregate_dsn(sources: list[str]) -> dict:
    """对多封完整原始 DSN 邮件做逐收件人投递状态归并。

    参数为完整原始邮件字符串列表（页面粘贴可先用
    :func:`split_batch_headers` 切分）。返回页面与
    ``POST /api/dsn/aggregate`` 共用的结构化结果。某封缺少投递状态
    部分或无法解析时记入 ``message_errors``，其余邮件继续归并；
    没有任何一封可解析出收件人时返回带 ``error`` 的字典（调用方据此
    返回 400）。
    """
    message_errors: list[dict] = []
    groups: list[dict] = []
    group_by_key: dict[tuple[str, str], dict] = {}
    ungrouped: list[dict] = []
    recipient_event_count = 0

    for mail_index, source in enumerate(sources, start=1):
        result = inspect_dsn(source)
        if "error" in result:
            message_errors.append({"index": mail_index, "error": result["error"]})
            continue

        for recipient in result["recipients"]:
            recipient_event_count += 1
            event = _event_view(recipient, mail_index, recipient_event_count)

            reasons: list[str] = []
            codes: list[str] = []
            oeid = recipient.get("original_envelope_id")
            oeid_status = recipient.get("original_envelope_id_status")

            if recipient["result"] == RESULT_REVIEW:
                codes.append("recipient_review")
                detail = "；".join(recipient.get("review_reasons") or []) or "字段缺失或自相矛盾"
                reasons.append(
                    f"该收件人判读结果为“待核对”（{detail}），"
                    "状态类别不明确，不参与归并"
                )

            if oeid_status != OEID_OK:
                codes.append(f"envelope_id_{oeid_status}")
                template = _OEID_REASON_LABELS.get(oeid_status)
                if template:
                    reasons.append(
                        template.format(
                            count=recipient.get("original_envelope_id_count", 0)
                        )
                    )

            if not recipient.get("final_recipient_raw"):
                codes.append("missing_final_recipient")
                reasons.append(
                    "Final-Recipient 字段缺失或原值为空，无法组成投递键"
                )

            if reasons:
                ungrouped.append(
                    {
                        "mail_index": mail_index,
                        "event_index": recipient_event_count,
                        "final_recipient": recipient.get("final_recipient"),
                        "final_recipient_raw": recipient.get("final_recipient_raw"),
                        "original_envelope_id": oeid,
                        "original_envelope_id_status": oeid_status,
                        "result": recipient["result"],
                        "result_label": recipient["result_label"],
                        "reason_codes": codes,
                        "reasons": reasons,
                        "recipient": event,
                    }
                )
                continue

            key = (oeid, recipient["final_recipient_raw"])
            group = group_by_key.get(key)
            if group is None:
                group = _make_group(key, event)
                group_by_key[key] = group
                groups.append(group)
            else:
                _append_event(group, event)

    for idx, group in enumerate(groups, start=1):
        group["index"] = idx

    analyzed_count = len(sources) - len(message_errors)
    if recipient_event_count == 0:
        return {
            "error": "所有邮件均未提供可归并的收件人投递状态："
                     "请确认每封都是含 message/delivery-status 部分的"
                     "完整原始退信回执。",
            "message_count": len(sources),
            "analyzed_count": analyzed_count,
            "message_errors": message_errors,
            "recipient_event_count": 0,
            "group_count": 0,
            "ungrouped_count": 0,
            "conflict_count": 0,
            "closed_count": 0,
            "open_deferred_count": 0,
            "final_success_count": 0,
            "final_deferred_count": 0,
            "final_permanent_count": 0,
            "final_conflict_count": 0,
            "groups": [],
            "ungrouped": [],
        }

    final_counts = {
        RESULT_SUCCESS: 0,
        RESULT_DEFERRED: 0,
        RESULT_PERMANENT: 0,
        FINAL_CONFLICT: 0,
    }
    closed_count = 0
    for group in groups:
        final_counts[group["final_result"]] += 1
        if group["closed"]:
            closed_count += 1

    return {
        "message_count": len(sources),
        "analyzed_count": analyzed_count,
        "message_errors": message_errors,
        "recipient_event_count": recipient_event_count,
        "group_count": len(groups),
        "ungrouped_count": len(ungrouped),
        "conflict_count": final_counts[FINAL_CONFLICT],
        "closed_count": closed_count,
        "open_deferred_count": final_counts[RESULT_DEFERRED],
        "final_success_count": final_counts[RESULT_SUCCESS],
        "final_deferred_count": final_counts[RESULT_DEFERRED],
        "final_permanent_count": final_counts[RESULT_PERMANENT],
        "final_conflict_count": final_counts[FINAL_CONFLICT],
        "groups": groups,
        "ungrouped": ungrouped,
        "notice": "归并以（Original-Envelope-Id，Final-Recipient 原值）为投递键："
                  "信封标识在其投递状态部分内必须唯一且非空，绝不只凭收件地址合并；"
                  "标识缺失 / 为空 / 重复或收件人判读为“待核对”的事件列入未归并清单。"
                  "每组按输入顺序展示状态轨迹，暂缓可由成功或永久失败结案；"
                  "结案后再出现不同类别状态时标记为冲突，原结案结论不静默覆盖。",
    }
