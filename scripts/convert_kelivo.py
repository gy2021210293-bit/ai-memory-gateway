#!/usr/bin/env python3
"""
Kelivo chats.json → 数据库 conversations 格式转换脚本

用法：
  python3 convert_kelivo.py chats.json -o output.json
  python3 convert_kelivo.py chats.json --after 2026-05-01
"""

import json
import argparse
from datetime import datetime, timedelta, timezone


def convert(input_path: str, output_path: str, tz_offset: int = 8, after_date: str = None):
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    conversations = data.get("conversations", [])
    messages_list = data.get("messages", [])

    # 解析 --after 日期
    cutoff_dt = None
    if after_date:
        local_tz = timezone(timedelta(hours=tz_offset))
        cutoff_dt = datetime.strptime(after_date, "%Y-%m-%d").replace(tzinfo=local_tz)

    # 建立 message id → message 的索引
    msg_by_id = {m["id"]: m for m in messages_list}

    # 建立 groupId → [messages] 的索引（用于处理多版本）
    groups = {}
    for m in messages_list:
        gid = m.get("groupId")
        if gid:
            groups.setdefault(gid, []).append(m)

    local_tz = timezone(timedelta(hours=tz_offset))
    output = []

    for conv in conversations:
        conv_id = conv["id"]
        conv_title = conv.get("title", "")
        version_selections = conv.get("versionSelections", {})
        message_ids = conv.get("messageIds", [])

        # 确定每个 groupId 应该选哪个 version
        # versionSelections: { groupId: selected_version }
        # 不在 versionSelections 里的 → 取 version 0

        seen_groups = set()

        for mid in message_ids:
            msg = msg_by_id.get(mid)
            if not msg:
                continue

            gid = msg.get("groupId") or mid

            # 同一个 groupId 只处理一次
            if gid in seen_groups:
                continue
            seen_groups.add(gid)

            # 找到这个 group 里被选中的那条消息
            group_msgs = groups.get(gid, [msg])
            selected_version = version_selections.get(gid, 0)

            chosen = None
            for gm in group_msgs:
                if gm.get("version") == selected_version:
                    chosen = gm
                    break
            if not chosen:
                # fallback：取 version 最大的
                chosen = max(group_msgs, key=lambda m: m.get("version", 0))

            content = chosen.get("content") or ""
            if not content.strip():
                continue

            # 时间转换：Kelivo 本地时间 → UTC（ISO 8601）
            ts_str = chosen.get("timestamp", "")
            if ts_str:
                try:
                    local_dt = datetime.fromisoformat(ts_str).replace(tzinfo=local_tz)
                    utc_dt = local_dt.astimezone(timezone.utc)
                    created_at = utc_dt.isoformat()
                    
                    # --after 过滤
                    if cutoff_dt and local_dt < cutoff_dt:
                        continue
                except (ValueError, TypeError):
                    created_at = ts_str
            else:
                created_at = None

            # model 处理
            model = chosen.get("modelId")
            if model == "None" or model is None:
                model = None
            # 标准化 model 名称（加 provider 前缀）
            elif model and "/" not in model:
                if model.startswith("gemini"):
                    model = f"google/{model}"

            # session_id：取 UUID 前 8 位（与数据库格式一致）
            session_id = conv_id.split("-")[0]

            output.append({
                "session_id": session_id,
                "role": chosen["role"],
                "content": content,
                "model": model,
                "created_at": created_at,
                "deleted_at": None,
                "_source_conv_title": conv_title,  # 额外标记，方便检查
            })

    # 按 created_at 排序（同一 session 内保持原始顺序）
    # 实际上 messageIds 顺序已经是正确的，这里不需要再排

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 统计
    sessions = {}
    for r in output:
        sid = r["session_id"]
        sessions.setdefault(sid, {"title": r["_source_conv_title"], "count": 0})
        sessions[sid]["count"] += 1

    print(f"转换完成：{len(output)} 条消息，{len(sessions)} 个 session")
    for sid, info in sessions.items():
        print(f"  {sid} ({info['title']}): {info['count']} 条")
    print(f"输出到：{output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kelivo → 数据库对话记录转换")
    parser.add_argument("input", help="Kelivo chats.json 文件路径")
    parser.add_argument("-o", "--output", default="converted_conversations.json", help="输出文件路径")
    parser.add_argument("--tz", type=int, default=8, help="Kelivo 本地时区偏移（默认 UTC+8）")
    parser.add_argument("--after", type=str, default=None, help="只转换此日期之后的记录（格式：YYYY-MM-DD）")
    args = parser.parse_args()
    convert(args.input, args.output, args.tz, args.after)
