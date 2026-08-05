"""
Drivesoid 情感引擎集成
=====================
环境变量 DRIVESOID_URL 配置后自动启用。
转发前读 /api/drives/context 注入当前 user 消息（不注入 system：
情绪每轮都变，注入 system 会让整段前缀缓存失效、历史全部不命中），
回复后发 msg_user + msg_assistant 事件驱动 16 维变化。
"""

import os
import httpx

DRIVESOID_URL = os.getenv("DRIVESOID_URL", "").strip().rstrip("/")
DRIVESOID_KEY = os.getenv("DRIVESOID_KEY", "").strip()


def is_enabled() -> bool:
    return bool(DRIVESOID_URL)


def _headers():
    h = {}
    if DRIVESOID_KEY:
        h["X-Drives-Key"] = DRIVESOID_KEY
    return h


async def fetch_context() -> str | None:
    """读取 [drives] block，stale 或出错返回 None"""
    if not DRIVESOID_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{DRIVESOID_URL}/api/drives/context", headers=_headers())
            if r.status_code == 200 and r.text.strip():
                return r.text.strip()
    except Exception as e:
        print(f"⚠️  [Drivesoid] context 读取失败: {e}")
    return None


def inject(messages: list, drives_text: str) -> bool:
    """
    注入到"最后一条且是 user"的消息末尾（与时间/记忆注入同位置）。
    只动本轮真实输入消息：它本来就不进缓存前缀，情绪每轮变化也不影响历史命中。
    绝不改历史消息或 system —— 那会让整段前缀失效。
    工具链请求（末尾是 tool 结果）没有当前 user 消息，本次跳过注入。
    """
    if not drives_text:
        return False
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content", "")
        if isinstance(content, str):
            messages[-1]["content"] = content + f"\n\n{drives_text}"
            print("💓 [Drivesoid] 情感状态已注入当前 user 消息")
            return True
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = block.get("text", "") + f"\n\n{drives_text}"
                    print("💓 [Drivesoid] 情感状态已注入当前 user 消息")
                    return True
    print("💓 [Drivesoid] 无当前 user 消息（工具链请求），本次跳过情感注入")
    return False


async def report_events(user_msg: str, assistant_msg: str):
    """上报 msg_user（触发 LLM 分类）+ msg_assistant（启动等待计时器）"""
    if not DRIVESOID_URL:
        return
    h = {"Content-Type": "application/json", **_headers()}
    async with httpx.AsyncClient(timeout=10) as c:
        if user_msg:
            try:
                r = await c.post(
                    f"{DRIVESOID_URL}/internal/drives/event",
                    json={"type": "msg_user", "payload": {"text": user_msg[:1000]}},
                    headers=h,
                )
                if r.status_code == 200:
                    print("💓 [Drivesoid] msg_user 已上报")
                else:
                    print(f"⚠️  [Drivesoid] msg_user HTTP {r.status_code}: {r.text[:200]}")
            except Exception as e:
                print(f"⚠️  [Drivesoid] msg_user 上报失败: {e}")
        try:
            r = await c.post(
                f"{DRIVESOID_URL}/internal/drives/event",
                json={"type": "msg_assistant", "payload": {}},
                headers=h,
            )
            if r.status_code == 200:
                print("💓 [Drivesoid] msg_assistant 已上报")
            else:
                print(f"⚠️  [Drivesoid] msg_assistant HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"⚠️  [Drivesoid] msg_assistant 上报失败: {e}")
