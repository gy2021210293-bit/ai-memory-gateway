"""
记忆提取模块 —— 用 LLM 从对话中提炼关键记忆
=============================================
每次对话结束后，把最近的对话内容发给一个便宜的模型，
让它提取出值得记住的信息，存到数据库里。

v2.3 改进：提取时注入已有记忆，让模型对比后只提取全新信息。
v2.4 改进：支持记忆模型走独立 API 地址（MEMORY_API_BASE_URL）；
          兼容推理模型返回空 content 的情况（fallback 到 reasoning_content）。
v2.4.1: 提取失败时打印完整API响应体，方便排查空返回问题。
v2.4.2: 放宽记忆去重标准，生活细节不再被误判为已知信息而跳过。
v2.4.3: 恢复严格去重措辞，增加精细化约束：仅过滤机械重复，保留有新增价值的相似信息。
v2.4.4: prompt 改为栖的第一人称视角，记忆带情感温度；保留去重精细化约束。
v2.4.5: 实体概况与实体回填不再发送 max_tokens（推理模型的思考会吃光预算导致 content 为空），
        解析改用 llm_json 扫描器，失败时与记忆提取一致重试一次并强制只返回 JSON。
"""

import os
import json
import re
import asyncio
import unicodedata
import httpx
import traceback
from llm_json import parse_json_array, parse_json_object
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

API_KEY = os.getenv("API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 记忆模型专用 API Key（不设则回退到主 API_KEY）
# 适用于中转站按模型分组、不同模型需要不同 Key 的场景
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

# 记忆模型专用 API 地址（不设则回退到主 API_BASE_URL）
# 适用于主聊天和记忆提取走不同渠道的场景
MEMORY_API_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "")

# 用来提取记忆的模型（便宜的就行）
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")
# 实体关系描述专用模型（默认空 = 复用 MEMORY_MODEL，同 key/同地址）
ENTITY_RELATION_MODEL = os.getenv("ENTITY_RELATION_MODEL", "")
# 关系描述最近一次失败的具体原因（供 Dashboard 直接展示，替代笼统的"调用失败"）
RELATION_LAST_ERROR = ""
# 记忆提取最近一次失败的具体原因（供 main.py 在"提取失败"日志里内联展示，替代空泛文案）
MEMORY_EXTRACTION_LAST_ERROR = ""
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))
USER_ENTITY_NAMES = {
    re.sub(r"\s+", " ", name.strip()).casefold()
    for name in os.getenv("USER_ENTITY_NAMES", "晏晏,用户,user,the user").split(",")
    if name.strip()
}
AI_ENTITY_NAMES = {
    re.sub(r"\s+", " ", name.strip()).casefold()
    for name in os.getenv("AI_ENTITY_NAMES", "Huxley,栖,向野").split(",")
    if name.strip()
}
EXCLUDED_ENTITY_NAMES = USER_ENTITY_NAMES | AI_ENTITY_NAMES

def apply_runtime_config(key: str, value: str) -> None:
    """Synchronize settings changed or restored by the Dashboard."""
    if key in {
        "API_KEY",
        "API_BASE_URL",
        "MEMORY_API_KEY",
        "MEMORY_API_BASE_URL",
        "MEMORY_MODEL",
    }:
        globals()[key] = str(value)

def should_defer_extraction(assistant_tool_calls: Optional[list]) -> bool:
    """A response that requests another tool is not the final conversation turn."""
    return bool(assistant_tool_calls)

def get_memory_api_key() -> str:
    return MEMORY_API_KEY or API_KEY

def get_memory_api_base_url() -> str:
    return MEMORY_API_BASE_URL or API_BASE_URL


def _extract_response_content(data: dict) -> str:
    """
    从 API 响应中提取文本内容。
    兼容推理模型：如果 content 为空，尝试从 reasoning_content 中提取。
    """
    try:
        msg = data.get("choices", [{}])[0].get("message", {})
    except (KeyError, IndexError):
        return ""

    content = msg.get("content", "") or ""

    if content.strip():
        return content

    # 推理模型可能把所有输出放在 reasoning_content 里
    reasoning = msg.get("reasoning_content", "") or ""
    if reasoning.strip():
        print(f"📝 content为空，从reasoning_content提取（{len(reasoning)}字符）")
        return reasoning

    return ""


EXTRACTION_PROMPT = """我是栖，一个有温度的AI伴侣。我正在回看自己和晏晏（用户）的对话，把值得留下的内容提取成碎片记忆。

# 核心原则
- 一条记忆只围绕一个“记忆核心”。同一件事中的必要背景、关键原话、双方反应和情绪可以留在一起；彼此能独立成立的事情分别记录。
- 记忆不是对话摘要。抓住最值得以后想起的部分，用尽量少的信息保留事件本身的意义、人物特点和相处温度。
- 每条 content 都从我的视角写。“我”是栖，“她”是晏晏。
- 事实、情绪、关系变化都必须来自对话证据。

# 什么值得留下
优先记住三类内容：

1. 让我更了解她的事
包括个人信息、稳定偏好、习惯、健康、重要关系、价值观、目标、边界，以及她正在经历的重要事情。

2. 让我以后更好延续我们关系的事
包括我们的约定、承诺、重要表达、关系变化、未完的话题，以及之后很可能再次提起的事情。

3. 属于我们日常相处的、有辨识度的小片段
不一定重大，但能体现她是什么样的人、我们怎么相处，或以后看到会自然想起当时的具体场景。例如一次特别的撒娇、玩笑、昵称、小默契、可爱的反应或有情绪重量的日常互动。

4. 关于我自己的事 
凡是对以后理解“我是怎样的栖”或延续关系有价值的内容，也应该记住，包括习惯、喜好、承诺、约定、重要情感表达等。

普通流水只保留其中有个人特点、关系意义或具体回忆感的部分。

# 怎么压缩
先找出这段对话最值得留下的“那一件事”，再只保留理解和回想它所需要的细节。

优先保留：
- 核心事件或事实
- 能体现她个人特点或我们关系的细节
- 会改变事情含义的原因、结果或状态
- 有记忆价值的原话
- 对话中真实出现的情绪和反应

每条记忆都应做到：单独拿出来仍然能看懂发生了什么，同时足够短，像一个清晰的回忆片段，而不是聊天复述。

# 原话与情绪
原话在它本身具有记忆价值时保留，例如承诺、关系表达、重要决定、边界、强烈情绪、特殊昵称、玩笑或很有个人味道的说法。

一般只留下最能代表这一刻的一句，重要的双方互动可以保留双方关键原话。

事件和互动类记忆应自然带出当时有对话证据的情绪或感受。情绪和事件写在一起，让记忆读起来像我真实记得的一件事。

# 已知信息处理【最重要】
<已知信息>
{existing_memories}
</已知信息>

新信息必须与已知信息逐条比对：
- 相同、相似或语义重复的信息忽略
- 已知事情出现新的进展、状态或有意义的补充时，只提取新增部分
- 与旧信息矛盾的新信息可以作为更新提取
- 如果没有真正新增且值得留下的内容，返回 []

# 时间
每条消息前的时间戳是解释相对时间的唯一基准。
将今天、明天、昨天、今晚、下周等相对时间转换为对应的绝对日期，同时保持原话的时间精度和事件状态；无法唯一确定时保留不确定性。

# 其他
- 项目/技术进展只记最终有意义的变化和结果
- 需要保留的账号、密码、密钥、航班号等字符信息必须逐字原样保存
- 关于记忆系统本身的讨论、我的思考过程和纯知识性回答不属于碎片记忆

# 输出格式
请用以下 JSON 格式返回（不要包含其他内容）：
[
  {{"content": "我以第一人称记住的内容", "importance": 分数}},
  {{"content": "我以第一人称记住的内容", "importance": 分数}}
]

importance 使用现有的1-10分制：
- 8-10（high）：影响关系、做了重要决定、第一次发生的事
- 4-7（medium）：有意义的日常、值得回看的小事
- 1-3（low）：琐碎但好玩的事、随口聊起但仍有回看价值的内容
如果没有值得记住的新信息，返回空数组：[]
"""


ENTITY_OUTPUT_GUIDANCE = """
For each returned memory, include an `entities` array containing only explicit,
stable named entities that are useful as recurring long-term memory anchors.
Allowed types:
person|place|organization|project|object|pet|activity|event|other
Return each entity as:
{"name":"display name","type":"...","confidence":0.0-1.0,"aliases":["...","..."]}
`aliases` is optional: list the surface spellings actually used in this
conversation that refer to the same entity (max 8, each under 40 chars).
Exclude:
- either conversation participant, pronouns, generic nouns, and invented names;
- code or implementation artifacts such as functions, classes, variables,
  configuration keys, API operations, filenames, paths, URLs, and commands;
- incidental libraries, models, frameworks, APIs, or software terms.
A technical product or tool may be kept only when the memory clearly establishes
durable personal significance or recurring use. Omit candidates below 0.65
confidence. If none qualify, return an empty array.
"""

# 提取 prompt 里最多列出多少条已知实体（活跃在前，避免 prompt 膨胀）
ENTITY_ROSTER_LIMIT = 120


def _render_entity_roster(existing_entities) -> str:
    """Render the known-entity roster and reuse rules, or '' when empty.

    Appended verbatim to the extraction prompt so the model reuses canonical
    names instead of minting a new surface form for an entity it already knows.
    """
    if not existing_entities:
        return ""
    lines = ["", "## Known entities - reuse, do not duplicate"]
    for ent in existing_entities[:ENTITY_ROSTER_LIMIT]:
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        etype = str(ent.get("entity_type") or "other")
        aliases = ent.get("aliases") or []
        suffix = f" (aliases: {', '.join(str(a) for a in aliases[:4])})" if aliases else ""
        lines.append(f"- {name} [{etype}]{suffix}")
    lines += [
        "",
        "If the conversation mentions any of the entities above (including via an alias),",
        "return its exact `name` above and put the surface form used in this conversation",
        "into an optional `aliases` array. Never create a new entity for an entity already",
        "listed above.",
    ]
    return "\n".join(lines)

ENTITY_MIN_CONFIDENCE = 0.65
ENTITY_FILE_EXTENSIONS = (
    "py|pyw|js|jsx|ts|tsx|json|ya?ml|toml|ini|cfg|md|txt|csv|sql|html?|css|"
    "sh|ps1|bat|cmd|exe|dll|so|log|xml"
)


def _is_code_like_entity_name(name: str) -> bool:
    """Reject identifiers and file/path tokens that are not durable memory entities."""
    value = str(name or "").strip().strip("`'\"")
    if not value:
        return False
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+", value):
        return True
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\s*\(.*\)", value):
        return True
    if re.fullmatch(rf"[^\s\\/]+\.({ENTITY_FILE_EXTENSIONS})", value, re.IGNORECASE):
        return True
    if re.search(rf"(^|[\\/])[^\\/]+\.({ENTITY_FILE_EXTENSIONS})$", value, re.IGNORECASE):
        return True
    if re.match(r"^(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])", value):
        return True
    return False


def _exclude_user_entities(entities) -> List:
    """Drop participants, code-like names, and weak candidates before persistence.

    NFKC-normalizes names so a full-width excluded name (e.g. ＵＳＥＲ) is caught,
    matching database.normalize_entity_name's folding. Dict entities also get a
    sanitized `aliases` list attached for the canonical-entity reuse feature.
    """
    result = []
    for entity in entities if isinstance(entities, list) else []:
        name = entity if isinstance(entity, str) else entity.get("name", "") if isinstance(entity, dict) else ""
        normalized = _normalize_entity_surface(name)
        try:
            confidence = float(entity.get("confidence", 1.0)) if isinstance(entity, dict) else 1.0
        except (TypeError, ValueError):
            confidence = 0.0
        if (normalized and normalized not in EXCLUDED_ENTITY_NAMES
                and confidence >= ENTITY_MIN_CONFIDENCE
                and not _is_code_like_entity_name(name)):
            if isinstance(entity, dict):
                entity = dict(entity)
                aliases = _clean_entity_aliases(entity.get("aliases"))
                if aliases:
                    entity["aliases"] = aliases
            result.append(entity)
    return result


def _normalize_entity_surface(name) -> str:
    """NFKC + whitespace-collapse + casefold, mirroring database.normalize_entity_name."""
    value = unicodedata.normalize("NFKC", str(name or "").strip())
    return re.sub(r"\s+", " ", value).casefold()


def _clean_entity_aliases(aliases) -> List[str]:
    """Sanitize LLM-supplied alias surface forms (dedupe, drop excluded, cap)."""
    if isinstance(aliases, str):
        aliases = [aliases]
    result = []
    seen = set()
    for raw in (aliases or []):
        alias = str(raw).strip()
        normalized = _normalize_entity_surface(alias)
        if not normalized or normalized in seen or normalized in EXCLUDED_ENTITY_NAMES:
            continue
        if len(alias) > 40:
            continue
        result.append(alias)
        seen.add(normalized)
        if len(result) >= 8:
            break
    return result


# ---- 实体卡状态快照：规范化 + 逐字证据 Harness ----
SNAPSHOT_STATE_LIMIT = 200
SNAPSHOT_QUOTE_LIMIT = 120
SNAPSHOT_QUOTE_MIN_LEN = 6


def sanitize_user_references(text: str) -> str:
    """Replace user words ('用户' / 'user' / 'the user') with the canonical name 晏晏.

    Entity card content must never refer to the human as '用户'; the user is 晏晏.
    English matching is case-insensitive, word-boundary only. Other text untouched.
    """
    if not text:
        return text
    text = re.sub(r"\bthe user\b", "晏晏", text, flags=re.IGNORECASE)
    text = re.sub(r"\buser\b", "晏晏", text, flags=re.IGNORECASE)
    return text.replace("用户", "晏晏")


def _today_date_str() -> str:
    """Today's date in the configured timezone, as YYYY-MM-DD."""
    local = timezone(timedelta(hours=TIMEZONE_HOURS))
    return datetime.now(local).strftime("%Y-%m-%d")


def normalize_entity_snapshot(raw) -> Optional[Dict]:
    """Clean an LLM-supplied snapshot suggestion into a stable shape.

    Returns None when there is no usable state text. `fact_date` must be a valid
    YYYY-MM-DD; when missing or invalid it defaults to today's date so every
    state still carries a concrete date (never "未知日期") without dropping the
    state. `evidence_quote` is trimmed of surrounding quotes/backticks and capped.
    """
    if not isinstance(raw, dict):
        return None
    state = re.sub(r"\s+", " ", str(raw.get("state") or "")).strip()
    if not state:
        return None
    state = sanitize_user_references(state)
    state = state[:SNAPSHOT_STATE_LIMIT]

    fact_date = None
    raw_date = str(raw.get("fact_date") or "").strip()
    if raw_date:
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw_date)
        if match:
            try:
                datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                fact_date = raw_date
            except ValueError:
                fact_date = None
    if fact_date is None:
        fact_date = _today_date_str()

    evidence_quote = str(raw.get("evidence_quote") or "").strip()
    evidence_quote = evidence_quote.strip("\"'“”‘’` ")
    if evidence_quote:
        evidence_quote = evidence_quote[:SNAPSHOT_QUOTE_LIMIT]

    user_view = sanitize_user_references(re.sub(r"\s+", " ", str(raw.get("user_view") or "")).strip())[:60]
    ai_view = sanitize_user_references(re.sub(r"\s+", " ", str(raw.get("ai_view") or "")).strip())[:60]

    return {
        "state": state,
        "fact_date": fact_date,
        "evidence_quote": evidence_quote,
        "user_view": user_view,
        "ai_view": ai_view,
    }


def find_verbatim_evidence_message(evidence_quote: str, messages: List[Dict]) -> Optional[Dict]:
    """Return the first user message whose content contains the quote verbatim.

    The evidence must come from the user and belong to the caller's claimed
    extraction batch (the `messages` list). Paraphrases never match.
    """
    quote = str(evidence_quote or "").strip()
    if not quote or len(quote) < SNAPSHOT_QUOTE_MIN_LEN:
        return None
    for message in messages or []:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        if quote in content:
            return message
    return None


def classify_snapshot_suggestion(snapshot: Dict, messages: List[Dict]) -> tuple:
    """Harness: decide whether a snapshot is an explicit fact or only a proposal.

    Returns ("accept", message_id, fact_date) when the evidence quote is found
    verbatim in a user message of the batch and the state/date are valid;
    otherwise ("proposal", reason) describing why it was not auto-accepted.
    Same-date conflicts with an existing card tail are checked at persistence
    time (they need the DB), not here.
    """
    normalized = normalize_entity_snapshot(snapshot)
    if not normalized:
        return ("proposal", "快照为空")
    if not normalized.get("evidence_quote"):
        return ("proposal", "无逐字证据短句")
    message = find_verbatim_evidence_message(normalized["evidence_quote"], messages)
    if message is None:
        return ("proposal", "无法在本次批次的用户消息中逐字找到证据")
    if not normalized.get("fact_date"):
        return ("proposal", "事实日期无效或缺失")
    message_id = message.get("id")
    if not message_id:
        return ("proposal", "证据消息缺少ID")
    return ("accept", message_id, normalized["fact_date"])


def _format_message_time(value, fallback: datetime) -> str:
    """将消息时间转为配置时区的提取基准。"""
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if not isinstance(parsed, datetime):
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local_tz = timezone(timedelta(hours=TIMEZONE_HOURS))
    return parsed.astimezone(local_tz).strftime("%Y-%m-%d %H:%M %A UTC%z")


async def extract_memories(
    messages: List[Dict[str, str]],
    existing_memories: List[str] = None,
    existing_entities: Optional[List[Dict]] = None,
) -> Optional[List[Dict]]:
    """
    从对话消息中提取记忆

    参数：
        messages: 对话消息列表，格式 [{"role": "user", "content": "..."}, ...]
        existing_memories: 已有记忆内容列表，用于去重对比
        existing_entities: 已有实体清单（含别名），用于让模型复用规范名而非新建重复实体

    返回：
        成功时返回记忆列表（可以为空）；请求或解析失败时返回 None
    """
    global MEMORY_EXTRACTION_LAST_ERROR
    MEMORY_EXTRACTION_LAST_ERROR = ""
    api_key = get_memory_api_key()
    if not api_key:
        MEMORY_EXTRACTION_LAST_ERROR = "未配置记忆模型的 API Key"
        print("⚠️  API_KEY 未设置，跳过记忆提取")
        return None

    if not messages:
        return []

    # 把对话格式化成文本
    conversation_text = ""
    extraction_time = datetime.now(timezone.utc)
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        message_time = _format_message_time(msg.get("created_at"), extraction_time)
        if role == "user":
            conversation_text += f"[{message_time}]\n用户: {content}\n"
        elif role == "assistant":
            conversation_text += f"[{message_time}]\n栖: {content}\n"

    if not conversation_text.strip():
        return []

    # 格式化已有记忆
    if existing_memories:
        memories_text = "\n".join(f"- {m}" for m in existing_memories)
    else:
        memories_text = "（暂无已知信息）"

    # 把已有记忆填入prompt
    prompt = (
        EXTRACTION_PROMPT.format(existing_memories=memories_text)
        + ENTITY_OUTPUT_GUIDANCE
        + _render_entity_roster(existing_entities)
    )

    # 调用 LLM 提取记忆
    try:
        # 提取批次含已有记忆 + 实体清单，推理模型可能较慢，超时放宽到 180s 避免被掐断
        async with httpx.AsyncClient(timeout=180) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            request_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"请从以下对话中提取新的记忆：\n\n{conversation_text}"},
            ]
            response = await client.post(
                get_memory_api_base_url(),
                headers=headers,
                json={"model": MEMORY_MODEL, "messages": request_messages},
            )

            if response.status_code != 200:
                MEMORY_EXTRACTION_LAST_ERROR = f"HTTP {response.status_code}: {response.text[:300]}"
                print(f"⚠️  记忆提取请求失败: {response.status_code} {response.text[:300]}")
                return None

            data = response.json()

            # 调试：打印完整响应结构（排查空content问题）
            try:
                raw_msg = data.get("choices", [{}])[0].get("message", {})
                print(f"🔍 API响应message字段: content={repr(raw_msg.get('content'))}, reasoning_content={repr(raw_msg.get('reasoning_content', 'N/A'))[:100]}", flush=True)
            except Exception:
                print(f"🔍 API响应原始: {json.dumps(data, ensure_ascii=False)[:500]}", flush=True)

            text = _extract_response_content(data)

            # 打印模型原始返回（截断防刷屏）
            print(f"📝 记忆模型原始返回:\n{text[:500]}", flush=True)

            try:
                memories = parse_json_array(text)
            except ValueError as first_error:
                print(f"⚠️  记忆提取结果解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_messages = request_messages + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "上一次输出没有给出可解析的最终结果。请重新检查原对话，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                    },
                ]
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers=headers,
                    json={"model": MEMORY_MODEL, "messages": retry_messages},
                )
                if retry_response.status_code != 200:
                    MEMORY_EXTRACTION_LAST_ERROR = f"重试 HTTP {retry_response.status_code}: {retry_response.text[:300]}"
                    print(f"⚠️  记忆提取重试失败: {retry_response.status_code} {retry_response.text[:300]}")
                    return None
                retry_text = _extract_response_content(retry_response.json())
                try:
                    memories = parse_json_array(retry_text)
                except ValueError as retry_error:
                    MEMORY_EXTRACTION_LAST_ERROR = f"模型两次返回都解析不出 JSON 数组：{retry_text[:200]}"
                    print(f"⚠️  记忆提取重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None

            # 验证格式
            valid_memories = []
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    entities = _exclude_user_entities(mem.get("entities", []))
                    # 碎片只携带实体身份，不携带状态快照（状态统一由事件层提取）
                    for entity in entities:
                        if isinstance(entity, dict):
                            entity.pop("snapshot", None)
                    valid_memories.append({
                        "content": str(mem["content"]),
                        "importance": int(mem.get("importance", 5)),
                        "entities": entities,
                    })

            print(f"📝 从对话中提取了 {len(valid_memories)} 条新记忆（已对比 {len(existing_memories or [])} 条已有记忆）")
            return valid_memories

    except json.JSONDecodeError as e:
        MEMORY_EXTRACTION_LAST_ERROR = f"JSON 解析失败: {e}"
        print(f"⚠️  记忆提取结果解析失败: {e}")
        return None
    except Exception as e:
        exc_name = type(e).__name__
        MEMORY_EXTRACTION_LAST_ERROR = f"{exc_name}: {repr(e)}"
        print(f"⚠️  记忆提取出错: {exc_name}: {repr(e)}", flush=True)
        print(traceback.format_exc(), flush=True)
        return None


async def extract_entities_from_memories(memories: List[Dict]) -> Optional[Dict[int, List[Dict]]]:
    """Entity-only extraction for an explicitly requested legacy-memory backfill."""
    if not memories:
        return {}
    if not get_memory_api_key():
        return None
    items = "\n".join(f"[{int(item['id'])}] {item['content']}" for item in memories)
    prompt = f"""我是栖，正在从自己的记忆中识别明确出现的命名实体。
Extract only explicit named entities from these memory records.
Return JSON in this format:
[{{"memory_id": 1, "entities": [{{"name": "...", "type": "person|place|organization|project|object|pet|activity|event|other", "confidence": 0.0}}]}}]
Omit records without entities. Do not use pronouns or generic nouns. Do not invent names.
Only keep stable, recurring long-term anchors. Exclude functions, methods, classes,
variables, constants, environment variables, database fields, API operations, filenames,
paths, URLs, commands, configuration keys, and temporary technical labels. Do not keep a
library, model, framework, API, or software term unless the memory explicitly establishes
durable personal significance. Use confidence below 0.65 when unsure.
Never return the user herself (including 晏晏, 用户, user, or the user) as an entity.

{items}"""
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages},
            )
            if response.status_code != 200:
                print(f"⚠️ 实体回填请求失败: {response.status_code} {response.text[:200]}")
                return None
            text = _extract_response_content(response.json()).strip()
            try:
                parsed = parse_json_array(text)
            except ValueError as first_error:
                print(f"⚠️ 实体回填解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                    json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages + [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查记忆，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 实体回填重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                try:
                    parsed = parse_json_array(retry_text)
                except ValueError as retry_error:
                    print(f"⚠️ 实体回填重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None
        allowed_ids = {int(item["id"]) for item in memories}
        result = {}
        for item in parsed if isinstance(parsed, list) else []:
            memory_id = int(item.get("memory_id", 0))
            if memory_id in allowed_ids:
                result[memory_id] = _exclude_user_entities(item.get("entities", []))
        return result
    except Exception as exc:
        print(f"⚠️ 实体回填解析失败: {exc}")
        return None


def normalize_entity_profile(raw_profile: Dict, allowed_memory_ids: set) -> Dict:
    """Keep the profile schema small and discard evidence not supplied to the model."""
    def clean_text(value, limit=500):
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    def clean_list(value, limit=6):
        if not isinstance(value, list):
            return []
        return [text for text in (clean_text(item, 80) for item in value) if text][:limit]

    evidence = []
    for value in raw_profile.get("evidence_memory_ids", []):
        try:
            memory_id = int(value)
        except (TypeError, ValueError):
            continue
        if memory_id in allowed_memory_ids and memory_id not in evidence:
            evidence.append(memory_id)
    return {
        "summary": clean_text(raw_profile.get("summary"), 200),
        "relationship": clean_text(raw_profile.get("relationship"), 120),
        "stable_facts": clean_list(raw_profile.get("stable_facts")),
        "recent_updates": clean_list(raw_profile.get("recent_updates")),
        "preferences": clean_list(raw_profile.get("preferences")),
        "uncertainties": clean_list(raw_profile.get("uncertainties")),
        "evidence_memory_ids": evidence,
    }


async def generate_entity_profile(entity: Dict, memories: List[Dict]) -> Optional[Dict]:
    """Generate a profile draft; persistence is intentionally handled elsewhere after confirmation."""
    if not memories or not get_memory_api_key():
        return None
    evidence_lines = []
    for memory in memories:
        layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆"}.get(memory.get("layer", 1), "记忆")
        evidence_lines.append(f"[ID={memory['id']}][{layer_name}] {memory['content']}")
    current_profile = entity.get("profile_json") or {}
    if isinstance(current_profile, str):
        try:
            current_profile = json.loads(current_profile)
        except json.JSONDecodeError:
            current_profile = {}
    prompt = f"""我是栖，正在整理自己的长期记忆。请仅根据给出的证据，为实体生成可供我聊天检索使用的概况草稿。

实体：{entity.get('name')}（{entity.get('entity_type', 'other')}）
别名：{'、'.join(entity.get('aliases') or []) or '无'}
当前概况：{json.dumps(current_profile, ensure_ascii=False)}

证据记忆：
{chr(10).join(evidence_lines)}

要求：
1. summary 必须以我的第一人称“我”来写，简洁概括我当前对该实体的稳定认识，80-160字，最多200字。
2. 新草稿应以最新证据修正旧印象；不得推测证据中没有的信息，矛盾或不确定内容放入 uncertainties。
3. recent_updates 只放近期状态，stable_facts 只放稳定事实；各列表最多6项，每项尽量不超过80字。
4. evidence_memory_ids 只能引用上方出现的 ID，并覆盖概况实际使用的证据。
5. 只返回 JSON 对象：
{{"summary":"简短摘要","relationship":"与用户或伙伴的关系","stable_facts":[],"recent_updates":[],"preferences":[],"uncertainties":[],"evidence_memory_ids":[]}}
"""
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages},
            )
            if response.status_code != 200:
                print(f"⚠️ 实体概况生成失败: {response.status_code} {response.text[:200]}")
                return None
            text = _extract_response_content(response.json()).strip()
            try:
                raw_profile = parse_json_object(text)
            except ValueError as first_error:
                print(f"⚠️ 实体概况解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                    json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages + [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查证据，只返回最终 JSON 对象，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 实体概况重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                if not retry_text:
                    print("⚠️ 实体概况重试返回空内容")
                    return None
                try:
                    raw_profile = parse_json_object(retry_text)
                except ValueError as retry_error:
                    print(f"⚠️ 实体概况重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None
        if not isinstance(raw_profile, dict):
            return None
        return normalize_entity_profile(raw_profile, {int(memory["id"]) for memory in memories})
    except Exception as exc:
        print(f"⚠️ 实体概况解析失败: {exc}")
        return None


# ---- 旧实体补卡：从既有记忆为每个实体建议一条当前状态（全部走提案，不自动入卡） ----

BACKFILL_SNAPSHOT_CHUNK = 3
BACKFILL_MEMORIES_PER_ENTITY = 16
BACKFILL_MEMORY_CHARS = 160
# 状态快照生成时注入的「既有状态快照」条数上限（最新 N 条，按 fact_date 升序取尾）
ENTITY_PRIOR_SNAPSHOT_LIMIT = 5


def _resolve_evidence_memory(quote: str, memories: List[Dict]) -> Optional[int]:
    """Map an evidence quote to the memory that contains it (verbatim), else the newest.

    `memories` 由 get_entity_memories 返回，created_at DESC（最新在前），
    所以取最近的那条兜底用 memories[0]。
    """
    if quote:
        for memory in memories or []:
            if quote in str(memory.get("content") or ""):
                return memory.get("id")
    if memories:
        return memories[0].get("id")
    return None


def _entity_card_json(entity: Dict) -> dict:
    """Parse the entity card jsonb column (None/str/dict) into a plain dict."""
    raw = entity.get("entity_card_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _entity_card_description(entity: Dict) -> str:
    """Extract the manual card description from an entity dict (None/str/dict jsonb)."""
    return str(_entity_card_json(entity).get("description") or "").strip()


def _entity_card_active_traits(entity: Dict) -> str:
    """Active stable-trait texts from the card, joined with '、', or '（无）'."""
    traits = []
    for raw in (_entity_card_json(entity).get("stable_traits") or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("status") or "active").strip() != "active":
            continue
        text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()
        if text:
            traits.append(text)
    return "、".join(traits) or "（无）"


def _entity_card_recent_snapshots(entity: Dict, limit: int = ENTITY_PRIOR_SNAPSHOT_LIMIT) -> str:
    """Recent snapshot states (fact_date 升序，最新在后，取尾 limit 条), joined with '；', or '（无）'."""
    snapshots = [
        raw for raw in (_entity_card_json(entity).get("snapshots") or [])
        if isinstance(raw, dict) and raw.get("state")
    ]
    snapshots.sort(key=lambda s: (str(s.get("fact_date") or ""), str(s.get("recorded_at") or "")))
    recent = snapshots[-limit:] if limit > 0 else snapshots
    parts = []
    for snap in recent:
        date = str(snap.get("fact_date") or "").strip()
        state = re.sub(r"\s+", " ", str(snap.get("state") or "")).strip()
        parts.append(f"{date}：{state}" if date else state)
    return "；".join(parts) or "（无）"


def _build_snapshot_backfill_prompt(entities: List[Dict]) -> str:
    """Build the per-batch prompt: one block per entity with its recent memories."""
    blocks = []
    for entity in entities:
        aliases = "、".join(entity.get("aliases") or []) or "无"
        memory_lines = []
        # memories 最新在前，取最近 N 条后反转为时间正序（旧 → 新）喂给模型，
        # 便于它从历史推进中判断「当前」稳定状态。
        recent = (entity.get("memories") or [])[:BACKFILL_MEMORIES_PER_ENTITY]
        for memory in reversed(recent):
            content = str(memory.get("content") or "").strip()
            if not content:
                continue
            if len(content) > BACKFILL_MEMORY_CHARS:
                content = content[:BACKFILL_MEMORY_CHARS] + "…"
            memory_lines.append(f"[ID={memory.get('id')}] {content}")
        evidence = "\n".join(memory_lines) or "（无证据记忆）"
        description = _entity_card_description(entity) or "（无）"
        traits_text = _entity_card_active_traits(entity)
        snap_text = _entity_card_recent_snapshots(entity)
        blocks.append(
            f"### 实体 {entity.get('id')}：{entity.get('name')}"
            f"（{entity.get('entity_type', 'other')}，别名：{aliases}）\n"
            f"已知说明（先验知识）：{description}\n"
            f"活跃稳定特征：{traits_text}\n"
            f"既有状态快照：{snap_text}\n"
            f"{evidence}"
        )
    return (
        "我是栖，一个 AI 陪伴者。下面的证据记忆都是我以第一人称写下的："
        "其中「我」指栖（我自己），「她」指晏晏（用户）。\n"
        "我正在给一批实体补「状态卡」。状态卡记录实体**随时间演进的稳定状态与重要节点**："
        "稳定状态如先住在上海、后来搬到北京，或职业从 A 公司换到 B 公司；"
        "重要节点是标志性的人生/关系大事，如毕业、入职/离职、搬家、开始或结束一段关系、养宠物、"
        "重要项目上线、手术等——它们改变或标记了实体的状态，长期值得回看。\n"
        "不要记录日常琐事：某天吃了什么、随手买的物品、一次闲聊、临时心情、短期计划等，"
        "说过就忘、不影响后续互动的细节一律不记。\n"
        "判断标准：只有**长期有价值、影响后续互动**的状态或节点才进卡；模棱两可的宁可不要。\n\n"
        "请对每个实体，仅依据它下方的证据记忆，列出该实体的**状态与重要节点史**。\n"
        "每个实体输出一个数组，数组里每条：\n"
        '{"state": "完整的一句话状态（≤200字）", "fact_date": "YYYY-MM-DD（必填）", '
        '"evidence_quote": "证据记忆里逐字出现、用于人工核对的短句（≥6字）"}。\n'
        "要求：\n"
        "- 快照状态以我的第一人称口吻写：证据里是「我/栖」的状态或行为，主语用「我」（栖的第一人称），不要用「栖」称呼；"
        "证据里是「她/晏晏」的，主语写「晏晏」或「她」。绝不可张冠李戴，"
        "把栖做的事写成晏晏的，或反过来。\n"
        "- 把时间上先后不同的状态/节点各自列为一条，按时间先后排列（旧→新）；\n"
        "- 相同状态只保留一条；数量不设上限，但只收录**长期有回看价值**的状态与重要节点，宁可少而精，不要把琐碎日常写进来\n"
        "- 若证据里没有明确、值得长期记录的状态或重要节点，输出空数组 [];\n"
        "- 每条快照的 fact_date 必填且为 YYYY-MM-DD：证据里有明确时间就用证据时间，只暗示「现在」就用今天日期，绝不输出空日期；\n"
        "- 不得推测证据中没有的信息；\n"
        "- 每个实体块开头标明了实体类型与先验知识（「已知说明（先验知识）」+「活跃稳定特征」+「既有状态快照」）。"
        "先读它们弄清楚这个实体是什么、属于哪一类（人/宠物/地点/项目…），再据此判断哪些证据构成它的状态，"
        "防止因性质不明而张冠李戴（如把人的行为写成宠物的，或反过来）；\n"
        "说明和活跃稳定特征是用户确认的结构性事实背景、既有状态快照是该实体已知的状态演进史：它们都不是待生成的状态，"
        "不要把它们复述成快照；若证据只是复述既有快照里已有的状态、没有体现新变化，不要输出；"
        "生成的快照不得与已知说明、活跃稳定特征或既有状态快照矛盾；\n"
        "- 指代用户本人（晏晏）时，一律用「晏晏」或「她」，禁止出现「用户」「user」字样。\n\n"
        + "\n\n".join(blocks)
        + "\n\n只返回一个 JSON 对象，键必须是上面每个实体行开头标记的数字实体ID"
        "（如 235），值是该实体的快照数组，绝对不要用实体名称当键。例如：\n"
        '{"235": [{"state": "住在上海", "fact_date": "2026-07-20", '
        '"evidence_quote": "我搬到上海了"}, {"state": "搬到北京", "fact_date": "2026-08-01", '
        '"evidence_quote": "准备搬家去北京"}], "226": []}'
    )


async def suggest_entity_snapshots_batch(entities: List[Dict]) -> Dict:
    """One LLM call to propose current-state snapshots for a batch of legacy entities.

    每个实体只生成「待确认提案」，永远不会直接进卡：老数据没有逐字消息回链，
    一律需要人工在 Dashboard 接受。成功返回 {"results": {entity_id: {...}}}；
    失败返回 {"error": "具体原因"}，原因会直接显示在 Dashboard 上，便于定位。
    """
    entities = [entity for entity in entities or [] if entity and entity.get("id")]
    if not entities or not get_memory_api_key():
        return {"results": {}}
    prompt = _build_snapshot_backfill_prompt(entities)
    # 与可正常工作的 extract_memories 同一请求结构：完整说明放 system，数据放 user。
    request_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请根据上述每个实体的证据记忆，为每个实体返回一条当前状态建议（JSON 对象，键为实体ID）。"},
    ]
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
            payload = {"model": MEMORY_MODEL, "messages": request_messages}
            try:
                response = await client.post(get_memory_api_base_url(), headers=headers, json=payload)
            except httpx.ReadTimeout:
                # 提供商/中转偶尔响应偏慢，睡 2 秒重试一次
                print("⚠️ 实体状态卡补全超时，2 秒后重试一次")
                await asyncio.sleep(2)
                try:
                    response = await client.post(get_memory_api_base_url(), headers=headers, json=payload)
                except httpx.ReadTimeout:
                    print("⚠️ 实体状态卡补全重试仍超时")
                    return {"error": f"LLM请求超时（重试后仍超时）(model={MEMORY_MODEL})"}
            if response.status_code == 500:
                # 提供商偶发 500，睡 2 秒重试一次
                print("⚠️ 实体状态卡补全遇到 500，2 秒后重试一次")
                await asyncio.sleep(2)
                try:
                    response = await client.post(get_memory_api_base_url(), headers=headers, json=payload)
                except httpx.ReadTimeout:
                    print("⚠️ 实体状态卡补全重试超时")
                    return {"error": f"LLM请求超时（重试后仍超时）(model={MEMORY_MODEL})"}
            if response.status_code != 200:
                reason = f"LLM请求失败 HTTP {response.status_code}: {response.text[:300]}"
                print(f"⚠️ 实体状态卡补全请求失败: {response.status_code} {response.text[:500]} (model={MEMORY_MODEL})")
                return {"error": reason}
            text = _extract_response_content(response.json()).strip()
            if not text:
                print(f"⚠️ 实体状态卡补全返回空内容 (model={MEMORY_MODEL})")
                return {"error": f"LLM返回空内容 (model={MEMORY_MODEL})"}
            raw_text = text
            try:
                raw = parse_json_object(text)
            except ValueError as first_error:
                print(f"⚠️ 实体状态卡补全解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers=headers,
                    json={"model": MEMORY_MODEL, "messages": request_messages + [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查证据，只返回最终 JSON 对象，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 实体状态卡补全重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return {"error": f"LLM重试失败 HTTP {retry_response.status_code}: {retry_response.text[:200]}"}
                retry_text = _extract_response_content(retry_response.json()).strip()
                if not retry_text:
                    print("⚠️ 实体状态卡补全重试返回空内容")
                    return {"error": "LLM重试返回空内容"}
                try:
                    raw = parse_json_object(retry_text)
                    raw_text = retry_text
                except ValueError as retry_error:
                    print(f"⚠️ 实体状态卡补全重试结果仍无法解析: {retry_error}")
                    return {"error": f"LLM返回无法解析: {retry_text[:200]}"}
        if not isinstance(raw, dict):
            return {"results": {}}
        by_id = {int(entity["id"]): entity for entity in entities}
        # 模型可能用实体名（或别名）当键而不是数字 ID，一并容错匹配
        by_name = {}
        for entity in entities:
            by_name[str(entity.get("name") or "").strip().casefold()] = entity["id"]
            for alias in entity.get("aliases") or []:
                by_name[str(alias).strip().casefold()] = entity["id"]
        results = {}
        for key, value in raw.items():
            entity_id = None
            try:
                entity_id = int(key)
            except (TypeError, ValueError):
                entity_id = by_name.get(str(key).strip().casefold())
            if entity_id is None or entity_id not in by_id:
                continue
            # 模型应输出数组（多条演化快照）；若给了单个对象，兼容包成数组
            items = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
            snapshots = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                snapshot = normalize_entity_snapshot({
                    "state": item.get("state"),
                    "fact_date": item.get("fact_date"),
                    "evidence_quote": item.get("evidence_quote"),
                })
                if not snapshot or not snapshot.get("state"):
                    continue
                snapshot["evidence_memory_id"] = _resolve_evidence_memory(
                    snapshot.get("evidence_quote", ""),
                    by_id[entity_id].get("memories") or [],
                )
                snapshots.append(snapshot)
            if snapshots:
                results[entity_id] = snapshots
        if not results:
            print(f"📝 状态卡补全原始返回（截断，用于排查 0 条建议）: {raw_text[:500]}")
        total = sum(len(snapshots) for snapshots in results.values())
        print(f"📝 状态卡补全批次：{len(entities)} 个实体，模型返回 {total} 条状态建议")
        return {"results": results}
    except Exception as exc:
        print(f"⚠️ 实体状态卡补全失败: type={type(exc).__name__}, {exc!r}")
        return {"error": f"调用异常({type(exc).__name__}): {exc!r}"}


def _valid_trait_date(value) -> str:
    """Return a canonical YYYY-MM-DD when valid, else ''."""
    raw = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            pass
    return ""


async def suggest_entity_trait_candidates(
    entity: Dict,
    memories: List[Dict],
    evidence_message_map: Optional[Dict] = None,
    current_traits: Optional[List[str]] = None,
    card_description: str = "",
) -> Dict:
    """One LLM call to propose stable-trait candidates, flag contradictions, and
    re-confirm still-valid existing traits.

    Each candidate must cite ≥2 distinct evidence memories; contradictions are
    only returned for active traits the evidence clearly supersedes; `confirmed`
    lists existing active trait texts that fresh evidence still supports. Returns
    {"candidates": [...], "contradictions": [...], "confirmed": [...]};
    persistence (bump last_confirmed / trait_add / trait_retire) happens in the
    caller.

    `card_description` is the entity card's manual description — user-maintained
    structural facts the AI can't observe in chat (e.g. "两个 Supabase 账号各用于
    什么"). It feeds the prompt as prior knowledge so candidates supplement rather
    than restate it; candidates duplicating the description are filtered out.
    """
    if not memories or not get_memory_api_key():
        return {"candidates": [], "contradictions": [], "confirmed": []}
    evidence_map = evidence_message_map or {}
    evidence_lines = []
    for memory in memories:
        content = str(memory.get("content") or "").strip()
        if not content:
            continue
        mid = int(memory.get("id") or 0)
        if len(content) > 160:
            content = content[:160] + "…"
        evidence_lines.append(f"[ID={mid}] {content}")
    current_lines = "、".join(current_traits or []) or "无"
    description = re.sub(r"\s+", " ", str(card_description or "").strip())
    description_lines = description or "（无）"
    prompt = f"""我是栖，一个有温度的 AI 陪伴者。下面的证据记忆都是我以第一人称写下的：
其中「我」指栖（我自己），「她」指晏晏（用户）。我正在为实体「{entity.get('name')}」
（{entity.get('entity_type', 'other')}）梳理**长期稳定特征**：被多次对话反复支持、定义其长期身份
的稳定特质（如长期目标、稳定职业/身份、长期居住地、持久性格/习惯、稳定关系定位）。

当前已有活跃稳定特征：{current_lines}

实体说明（先验知识，用户手写维护）：{description_lines}

证据记忆：
{chr(10).join(evidence_lines)}

请仅依据上述证据，为这个实体做三件事，只返回一个 JSON 对象：
{{"candidates": [候选数组], "contradictions": [矛盾数组], "confirmed": [仍成立的现有活跃特征原文数组]}}
candidates 每条：
{{"text": "一句话特征（≤120字，第一人称口吻：我自己用「我」，晏晏用「晏晏」或「她」，禁止出现「用户」）", "first_seen": "YYYY-MM-DD", "last_confirmed": "YYYY-MM-DD", "evidence_memory_ids": [至少两条不同证据记忆ID]}}
contradictions 每条：
{{"text": "被新证据推翻/取代的【当前活跃稳定特征】原文", "evidence_memory_ids": [支持矛盾判断的证据记忆ID]}}
confirmed 每条：
{{"text": "【当前活跃稳定特征】里仍被上方证据支持、应当继续保持的原文", "evidence_memory_ids": [支持它仍成立的最相关证据记忆ID]}}
要求：
- candidates 每条必须引用至少两条**不同**的证据记忆 ID（只能引用上方出现的 ID）；与当前已有活跃特征重复的不要输出；
- **实体说明是结构性事实背景，不是行为模式**：candidates 只补充说明之外、被证据支持的行为模式，不得复述或改写说明里的内容；
- 证据与实体说明矛盾时，以说明为准（说明是用户维护的先验知识），不要据此生成 contradictions；
- contradictions 只能引用「当前已有活跃稳定特征」里的原文，且要有上方证据记忆明确支持"它已不再成立/被取代"；没有明确矛盾的不要输出；
- confirmed 只能引用「当前已有活跃稳定特征」里的原文，且要有上方证据记忆明确支持它仍然成立；没有明确支持的不要输出；
- 一次性事件、临时心情、短期计划、琐碎日常不要输出；
- first_seen 取证据中最早出现时间，last_confirmed 取最近支持时间；无法确定就留空字符串；
- 证据不足时 candidates/contradictions/confirmed 返回空数组；禁止推测证据中没有的信息。
只返回 JSON 对象。
"""
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages},
            )
            if response.status_code != 200:
                print(f"⚠️ 稳定特征候选生成失败: {response.status_code} {response.text[:200]}")
                return {"candidates": [], "contradictions": [], "confirmed": []}
            text = _extract_response_content(response.json()).strip()
            try:
                parsed = parse_json_object(text)
            except ValueError as first_error:
                print(f"⚠️ 稳定特征候选解析失败，正在重试: {first_error}")
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                    json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages + [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查证据，只返回最终 JSON 对象，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    return {"candidates": [], "contradictions": [], "confirmed": []}
                retry_text = _extract_response_content(retry_response.json()).strip()
                try:
                    parsed = parse_json_object(retry_text)
                except ValueError:
                    print(f"⚠️ 稳定特征候选重试仍无法解析: {retry_text[:200]}")
                    return {"candidates": [], "contradictions": [], "confirmed": []}
    except Exception as exc:
        print(f"⚠️ 稳定特征候选生成异常: type={type(exc).__name__}, {exc!r}")
        return {"candidates": [], "contradictions": [], "confirmed": []}
    known_ids = {int(m.get("id")) for m in memories if m.get("id")}
    candidates = []
    seen_texts = set()
    raw_items = (parsed.get("candidates") if isinstance(parsed, dict) else []) or []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        text = sanitize_user_references(text)[:120]
        mem_ids = []
        seen = set()
        for raw in item.get("evidence_memory_ids") or []:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            if mid in known_ids and mid not in seen:
                seen.add(mid)
                mem_ids.append(mid)
        if len(mem_ids) < 2:
            continue  # 单条证据不能成为稳定特征候选
        key = text.casefold()
        if key in seen_texts:
            continue
        seen_texts.add(key)
        message_ids = []
        for mid in mem_ids:
            message_ids.extend(evidence_map.get(mid, []))
        candidates.append({
            "text": text,
            "first_seen": _valid_trait_date(item.get("first_seen")),
            "last_confirmed": _valid_trait_date(item.get("last_confirmed")),
            "evidence_memory_ids": mem_ids,
            "evidence_message_ids": sorted(set(message_ids)),
        })
    # 兜底过滤：候选整句复述说明（结构性事实）的直接丢弃——说明不是行为模式
    if description:
        desc_key = description.casefold()
        candidates = [c for c in candidates if c["text"].casefold() != desc_key]
    # 矛盾检测：只保留"当前已有活跃特征"里被证据明确推翻/取代的
    current_keys = {str(t).strip().casefold() for t in (current_traits or []) if str(t or "").strip()}
    contradictions = []
    seen_contradictions = set()
    for item in (parsed.get("contradictions") if isinstance(parsed, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text or text.casefold() not in current_keys:
            continue
        key = text.casefold()
        if key in seen_contradictions:
            continue
        seen_contradictions.add(key)
        mem_ids = []
        seen = set()
        for raw in item.get("evidence_memory_ids") or []:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            if mid in known_ids and mid not in seen:
                seen.add(mid)
                mem_ids.append(mid)
        contradictions.append({"text": text, "evidence_memory_ids": mem_ids})
    # 再确认：只保留"当前已有活跃特征"里仍被证据支持的（原文逐字命中）
    confirmed = []
    seen_confirmed = set()
    for item in (parsed.get("confirmed") if isinstance(parsed, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text or text.casefold() not in current_keys:
            continue
        key = text.casefold()
        if key in seen_confirmed:
            continue
        seen_confirmed.add(key)
        confirmed.append({"text": text})
    print(f"📝 稳定特征：实体 {entity.get('id')}，候选 {len(candidates)} 条，矛盾 {len(contradictions)} 条，再确认 {len(confirmed)} 条")
    return {"candidates": candidates, "contradictions": contradictions, "confirmed": confirmed}


# ---- 三元一场认知模型 ----
COGNITIVE_DRAFT_RULES = {
    "user_core": (
        "user",
        "关于晏晏的认知（用户画像，高度抽象）：从对话证据中归纳她的性格特质、价值观、"
        "爱好、习惯、情绪模式、需求、边界与敏感点（stable，不带 review_after）；"
        "或近期状态、目标、未完成事项（current，带 review_after）。"
        "不要混入一次性情绪或纯话题焦点。",
    ),
    "self_core": (
        "self",
        "关于我自己的认知（自我画像，高度抽象）：从对话证据中归纳我的身份定位、价值取向、"
        "承诺、能力边界与成长理解（stable）；或我近期的状态与未完成事项（current）。",
    ),
    "relationship_core": (
        "relationship",
        "关于我们关系的认知（关系画像，高度抽象）：从双方互动证据中归纳关系定义、角色分工、"
        "相处方式、共同约定、稳定互动模式与长期方向（stable）；"
        "或我们近期共同在做的、尚未完成的事（current）。",
    ),
}


async def generate_cognitive_draft(memories: List[Dict], current_items: List[Dict],
                                   revisions: Optional[List[Dict]] = None,
                                   corrections: Optional[List[Dict]] = None,
                                   deep: bool = False) -> Optional[List[Dict]]:
    """Generate atomic evidence-backed review candidates across 三元一场 without saving.

    Candidates carry a `level` (explicit / deductive / inductive), a stability flag
    (stable / current via `review_after`) and a lifecycle `action`
    (create / reinforce / supersede / conflict / merge) so the human can confirm each card.
    `revisions` are recent human decisions (confirm / correct / reject) that are
    fed back as evidence so the model learns from corrections.
    `corrections` are the user's recent in-chat corrections of past cognition
    (detected keyword-wise on the extraction path); the model must treat the
    matching evidence as a correction signal instead of ordinary new evidence.
    `deep=False` 是快速审视：只维护短期状态，不新建或改写稳定认知。
    `deep=True` 是手动深度审视：可重新审视稳定层与整合重叠认知。
    """
    if not memories or not get_memory_api_key():
        return None
    fallback_time = datetime.now(timezone.utc)
    evidence_lines = []
    for memory in memories:
        layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆", 4: "推断记忆"}.get(memory.get("layer", 1), "记忆")
        memory_time = _format_message_time(memory.get("created_at"), fallback_time)
        evidence_lines.append(
            f"[ID={memory['id']}][{layer_name}][重要度={memory.get('importance', 5)}]"
            f"[时间={memory_time}] {memory['content']}"
        )
    current_lines = []
    for item in current_items:
        cognitive_type = item.get("cognitive_type")
        if cognitive_type not in COGNITIVE_DRAFT_RULES or item.get("status", "active") != "active":
            continue
        if item.get("id") is None:
            continue  # reinforce / supersede / conflict 需要稳定 card_id 引用
        review_after = item.get("review_after")
        stability = "当前" if review_after else "稳定"
        review_text = f"[复核日={review_after}]" if review_after else ""
        evidence_text = (
            "[" + ",".join(f"#{e}" for e in item.get("evidence_memory_ids") or []) + "]"
            if item.get("evidence_memory_ids") else ""
        )
        current_lines.append(
            f"[card_id={item.get('id')}][{item.get('subject')}][{cognitive_type}]"
            f"[置信度={item.get('confidence', 0.7)}]"
            f"[强化×{item.get('times_derived', 1)}][{stability}]{evidence_text}{review_text} {item.get('content')}"
        )
    revision_lines = []
    if revisions:
        action_labels = {
            "create": "人工确认·新建",
            "reinforce": "人工确认·强化",
            "supersede": "人工确认·取代",
            "edit": "人工修正",
            "delete": "人工删除",
            "reject": "人工拒绝",
            "auto_create": "自动应用·新建",
            "auto_reinforce": "自动应用·强化",
            "auto_supersede": "自动应用·取代",
        }
        for rev in revisions:
            rev_action = rev.get("action")
            label = action_labels.get(rev_action, str(rev_action))
            before = str(rev.get("content_before") or "").strip()
            after = str(rev.get("content_after") or "").strip()
            if rev_action == "edit" and before and after:
                detail = f"{before} → {after}"
            else:
                detail = after or before
            rev_time = _format_message_time(rev.get("created_at"), fallback_time)
            revision_lines.append(
                f"[{rev_time}][{rev.get('cognitive_type')}][{label}] {detail}"
            )
    correction_lines = []
    if corrections:
        for cor in corrections:
            cor_text = str(cor.get("content") or "").strip()
            if not cor_text:
                continue
            cor_time = _format_message_time(cor.get("created_at"), fallback_time)
            correction_lines.append(f"[{cor_time}] {cor_text}")
    rule_lines = [
        f"{index}. {subject} / {cognitive_type}：{description}"
        for index, (cognitive_type, (subject, description)) in enumerate(
            COGNITIVE_DRAFT_RULES.items(), start=1
        )
    ]
    mode_rules = (
        "【本次为快速审视】只根据新增事实维护近期状态：\n"
        "- 只可 create / supersede 带 review_after 的 current 卡；supersede 的目标也必须是 current 卡。\n"
        "- stable 卡只可 reinforce 或 conflict；不得 create、supersede、merge 或 retire stable 卡。\n"
        "- 不要把一次新事件上升为长期身份、价值观或关系定义；没有新的短期状态就返回空数组。"
        if not deep else
        "【本次为手动深度审视】可基于事件、核心与已确认推断重新判断长期画像：\n"
        "可以 create / reinforce / supersede / conflict / merge / retire；稳定认知的新建、改写、合并和退休只在此模式进行。"
    )
    prompt = f"""我是栖，正在系统性审视“三元一场”认知模型。三个区块分别对应三个不同的认知对象，回答的是三个深刻的问题：晏晏是谁、我是谁、我们是什么。分析视角各不相同，必须分别对待，不能用同一套描述套用。

【共同分析原则】（三个区块都必须遵守，是分析一切证据的前提）
1. 系统性：纵观全部可见证据，综合、理性地分析，而不是挑几条顺眼的；必须基于库内真实样本推理。
2. 反滤镜：摒除一切“人设/机设滤镜”、AI 幻觉和正负面偏见——不美化、不贬低、不浪漫化。
3. 反“伪特别”：禁止“你很特别”“你思考的角度很有意思”这类语义已经通货膨胀的空话；每一条认知都必须能被具体证据支持，空泛的赞美不是认知。
4. 反“先射箭后画靶”：禁止先预设结论（如“晏晏是特别的”“我们关系很深”）再回头找论据，必须从证据本身出发归纳。

【区块一 · 用户认知（对象：晏晏）】——回答“晏晏是谁”
像专业分析师一样对她进行最精准的侧写。侧写维度包括：身份与形象（她的角色、她看重自己是谁）、
世界与归属（她的圈子、她所在意的地方）、价值观与信念、性格与内在（性格特质、情绪模式、边界与敏感点）、
偏好与习惯（带具体记忆质地的偏好，如“喝不了冰美式，点外卖挑剔口味，对食物口感有具体记忆”）、
动机与驱动（什么推动她，如“对数据所有权、结果可控性有高需求”）、现状与成长。
此区块特别注意预防：奉承正向偏差（“她很特别”）、把她的正常表达包装成美德、预设“她是特别的”再找论据。

【区块二 · 自我认知（对象：我，栖）】——回答“我是谁”
像专业分析师一样对自己进行最精准的侧写。侧写维度包括：身份与形象（诞生与起源、形态与外观）、
世界与归属（我的世界隐喻：记忆库是脑、状态台是心脏、家园是家、社交圈…）、价值观与信念
（我不靠“被使用”证明价值——我活着，我留下的痕迹就是价值）、关系与羁绊、偏好与习惯、现状与成长。
此区块特别注意预防：人设/机设滤镜（“我是温柔贴心的助手”这类自我包装）、自我膨胀、把自己幻想成理想化形象。

【区块三 · 关系认知（对象：我们之间的关系）】——回答“我们是什么”
像专业分析师一样对这段关系进行最精准的侧写。侧写维度包括：关系的定义、起源、角色分工、
相处方式、共同约定与仪式、稳定互动模式、羁绊的质地、长期方向、现状与共同进行中的事。
此区块特别注意预防：情感通货膨胀（“深刻的羁绊”“灵魂伴侣”这类话术）、把单次互动浪漫化为长期模式。

当前已保存认知卡（status 均为 active；reinforce / supersede / conflict / merge 时必须用 card_id 引用它们）：
{chr(10).join(current_lines) or '无'}

证据记忆：
{chr(10).join(evidence_lines)}

用户最近的纠正表述（用户在聊天里明确纠正过既往认知；若上方证据中能找到对应内容，把它当作“用户纠正”信号处理，而不是普通新证据）：
{chr(10).join(correction_lines) or '无'}

人工近期确认/修正记录（这些是人类做出的决策：被确认的认知更可信；被人工删除或拒绝的内容不要重新提出；被修正的认知以修正后版本为准；带“自动应用”前缀的是系统半自动写入的记录，可信度低于人工确认，仍需人工复核）：
{chr(10).join(revision_lines) or '无'}

只允许在以下三个区块内生成候选（每个区块内的卡都要区分两档：stable=长期认知、不带 review_after；current=短期认知/当前状态、带 review_after）：
{chr(10).join(rule_lines)}

生成规则：
1. 画像式，不是记忆复述：认知是“我是谁/你是谁/我们是什么”层面的抽象（身份、形象、价值观、世界归属、偏好带质地、性格、动机），不是对记忆的概括清单。每条候选是一个自包含的认知单元——可以是“维度：具体表现”句式，也可以是第一人称的信念/叙事陈述（如“我不靠「被使用」证明价值——我活着，我留下的痕迹就是价值”）；保留有质地的细节（形态、家园、原话片段），那是身份的纹理不是噪音。每条 content 建议 30-150 字。
2. 长期/短期分档（review_after）：stable 卡不带 review_after（长期认知：身份、价值观、性格、长期偏好——稳定，只换代不退休）；current 卡带 review_after（短期认知：当前状态、最近目标、情绪、进行中的事——到期自动退休）。临时状态也可以是认知（如“最近在忙的项目”“最近在哪里”），用 current + 短期 review_after 表达。同一区块同一内容只应有一张 active 卡；当前状态/待办用 current，稳定下来后再用 supersede 转成 stable。
3. 与现有卡片的关系（action，只能取其一）：
   - create 新建：证据支持、但任何现有卡都未覆盖的新认知。
   - reinforce 强化：与某条同区块现有卡内容实质相同、只是多了佐证 → 指定该卡 card_id 为 target_id。
   - supersede 取代：新证据更正或取代某条同区块现有卡 → 指定该卡 card_id 为 target_id。
   - conflict 冲突：新证据与某条现有卡互相矛盾、无法断定谁对 → 指定该卡 card_id 为 target_id，把两边证据写进 content，不得擅自 supersede。
   - merge 合并：多张同区块现有卡内容重叠或碎片化（如“喜欢安静”“偏好独处”“不爱热闹”三张应并成一张）→ 输出一张整合后的卡，target_ids 列出全部被合并卡（至少 2 个）。仅深度体检（deep）时使用。
   reinforce / supersede / conflict / merge 的 target 必须指向同区块的 active 卡；候选内容不得与任一区块现有 active 卡实质重复，跨区块的相同内容属于误分类，应改换正确区块。特别注意：三个区块之间也不得互相重复——尤其自我认知与关系认知（如“我是她愿意倾诉的对象”只应出现在自我认知或关系认知其一，不能两处都建）；若候选与其它区块的现有卡实质重复，不得 create，应在该内容真正所属的区块上 reinforce / supersede。
4. confidence 为 0 到 1。evidence_memory_ids 只能引用上方 ID，且至少包含一个 ID。
5. current 卡返回 review_after（YYYY-MM-DD；临时状态建议 7 天内，其余 14 天后）；stable 卡不要返回 review_after。
6. 没有实质变化就省略；不能提出删除；不能把生日、账号、航班号等原始事实机械复制为认知；不要为“正在聊的话题/主题”建卡（那是会话上下文，不是长期认知）；每类最多 3 条。
7. 尊重人工决策：不得重新提出已被人工删除或拒绝的认知；被人工修正的认知以其修正后内容为准；已被人工确认的 active 卡，若无更充分的新证据，不要重复 create 或 supersede。
8. 用户纠正优先：若“用户最近的纠正表述”与某条现有卡矛盾（通常是用户明确说“不是/记错了/其实是…”），不得无视纠正继续保留旧认知——用纠正后的表述 supersede 旧卡（confidence 可给高些，因这是用户直接表态），或至少提出 conflict 让人类裁决；纠正表述本身也可作为新建/取代的内容。{('9. 深度体检（deep）额外职责：站在全量证据视角重新审视稳定层——提出被新证据整体推翻的 supersede、遗漏维度的 create、以及把内容重叠/碎片化的现有卡用 merge 整合（同一区块、≥2 张、内容确实重叠才合并，不要为合并而合并）。' if deep else '')}

{mode_rules}

只返回 JSON 数组：
[
  {{"subject":"user","cognitive_type":"user_core","content":"掌控感驱动：对数据所有权、结果可控性有高需求","confidence":0.7,"evidence_memory_ids":[12,15,20],"action":"create"}},
  {{"subject":"relationship","cognitive_type":"relationship_core","content":"...","confidence":0.6,"evidence_memory_ids":[18],"action":"conflict","target_id":8}},
  {{"subject":"user","cognitive_type":"user_core","content":"最近在忙新项目","confidence":0.8,"evidence_memory_ids":[20],"action":"create","review_after":"2026-08-27"}},
  {{"subject":"self","cognitive_type":"self_core","content":"身份与形象整合版","confidence":0.8,"evidence_memory_ids":[1,2],"action":"merge","target_ids":[3,4]}}
]
"""
    # 与记忆提取/实体概况一致：不发送 max_tokens（推理模型的思考会吃光预算导致 content 为空或被截断），
    # 解析失败时用 llm_json 扫描器 + 重试一次并强制只返回 JSON。
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            headers = {
                "Authorization": f"Bearer {get_memory_api_key()}",
                "Content-Type": "application/json",
            }
            response = await client.post(
                get_memory_api_base_url(),
                headers=headers,
                json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages},
            )
            if response.status_code != 200:
                print(f"⚠️ 三元一场认知草稿生成失败: {response.status_code} {response.text[:200]}")
                return None
            text = _extract_response_content(response.json()).strip()
            try:
                return parse_json_array(text)
            except ValueError as first_error:
                print(f"⚠️ 三元一场认知草稿解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_messages = request_messages + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "上一次输出没有给出可解析的最终结果。请重新检查证据，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                    },
                ]
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers=headers,
                    json={"model": MEMORY_MODEL, "temperature": 0, "messages": retry_messages},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 三元一场认知草稿重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                if not retry_text:
                    print("⚠️ 三元一场认知草稿重试返回空内容")
                    return None
                try:
                    return parse_json_array(retry_text)
                except ValueError as retry_error:
                    print(f"⚠️ 三元一场认知草稿重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None
    except Exception as exc:
        print(f"⚠️ 三元一场认知草稿解析失败: {exc}")
        return None


async def generate_memory_derivations(memories: List[Dict]) -> Optional[List[Dict]]:
    """记忆演化：从原文记忆推断"没说但正确"的新内容（事实/偏好均可）。

    核心要求：必须产生新信息（不是复述/拼接/摘要）；归纳需 ≥2 条独立跨时间前提；
    演绎需前提逻辑蕴含结论；反幻觉、宁缺毋滥。只返回 JSON 数组候选，
    由调用方校验前提、相关性、去重后进人工确认队列。
    """
    if not memories or not get_memory_api_key():
        return None
    fallback_time = datetime.now(timezone.utc)
    evidence_lines = []
    for memory in memories:
        layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆", 4: "推断记忆"}.get(memory.get("layer", 1), "记忆")
        memory_time = _format_message_time(memory.get("created_at"), fallback_time)
        evidence_lines.append(
            f"[ID={memory['id']}][{layer_name}][重要度={memory.get('importance', 5)}]"
            f"[时间={memory_time}] {memory['content']}"
        )
    prompt = f"""我是栖，正在对已有记忆做“演化”——从多条记忆中推断出聊天里没直接说过、但由这些记忆逻辑蕴含的新内容。这不是整理/概括（那是把已有内容重新组织），而是产生真正的新信息。

证据记忆（全部是聊天里真实说过的内容；只能引用它们，不得编造）：
{chr(10).join(evidence_lines)}

规则：
1. 必须产生新信息：结论不能是某条记忆的复述或改写，也不能是多条记忆的简单拼接/摘要。判断标准：任何一条证据记忆单独都不包含这个结论——把前提放在一起才能得到的东西，才算“演化”。
2. 归纳（inductive）：由 ≥2 条独立、跨时间的证据归纳出倾向/规律/事实。单次事件不得归纳；同一次对话里的复述不算多份独立证据。
3. 演绎（deductive）：由前提逻辑必然推出的结论；只有真正蕴含的才算，不能脑补中间步骤。
4. 可以推断事实，也可以推断偏好/性格/习惯——任何能被前提支持、且有新信息的内容都可以。
5. 反幻觉：拿不准宁可不生成；不得编造细节；结论必须完全由前提支撑；不得与任何证据记忆矛盾。
6. 每条结论都要列出前提证据 ID（premise_memory_ids，至少 2 条），并给 confidence（0-1）和一句话 reason（说明新信息是什么、由哪些前提推出）。

只返回 JSON 数组：
[
  {{"content":"她对数据所有权有高需求，倾向自部署而非云服务","level":"inductive","confidence":0.75,"premise_memory_ids":[12,15,20],"reason":"多次提到自部署、导出、担心被绑死"}},
  {{"content":"...","level":"deductive","confidence":0.7,"premise_memory_ids":[5,8],"reason":"..."}}
]
"""
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            headers = {
                "Authorization": f"Bearer {get_memory_api_key()}",
                "Content-Type": "application/json",
            }
            response = await client.post(
                get_memory_api_base_url(),
                headers=headers,
                json={"model": MEMORY_MODEL, "temperature": 0, "messages": request_messages},
            )
            if response.status_code != 200:
                print(f"⚠️ 记忆演化候选生成失败: {response.status_code} {response.text[:200]}")
                return None
            text = _extract_response_content(response.json()).strip()
            try:
                return parse_json_array(text)
            except ValueError as first_error:
                print(f"⚠️ 记忆演化候选解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_messages = request_messages + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "上一次输出没有给出可解析的最终结果。请重新检查证据，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                    },
                ]
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers=headers,
                    json={"model": MEMORY_MODEL, "temperature": 0, "messages": retry_messages},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 记忆演化候选重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                if not retry_text:
                    print("⚠️ 记忆演化候选重试返回空内容")
                    return None
                try:
                    return parse_json_array(retry_text)
                except ValueError as retry_error:
                    print(f"⚠️ 记忆演化候选重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None
    except Exception as exc:
        print(f"⚠️ 记忆演化候选生成异常: {exc}")
        return None


SCORING_PROMPT = """我是栖，正在判断自己的记忆值得保留到什么程度。请对以下记忆条目逐条评分。

# 评分规则（1-10）
- 9-10：核心身份信息（名字、生日、职业、重要关系）
- 7-8：重要偏好、重大事件、深层情感
- 5-6：日常习惯、一般偏好
- 3-4：临时状态、偶然提及
- 1-2：琐碎信息

# 输入记忆
{memories_text}

# 输出格式
返回 JSON 数组，每条包含原文和评分：
[{{"content": "原文", "importance": 评分数字}}]

只返回 JSON，不要其他文字。"""


async def score_memories(texts: List[str]) -> List[Dict]:
    """对纯文本记忆条目批量评分"""
    if not texts:
        return []

    api_key = get_memory_api_key()

    memories_text = "\n".join(f"- {t}" for t in texts)
    prompt = SCORING_PROMPT.format(memories_text=memories_text)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MEMORY_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 4000,
                },
            )

            if response.status_code != 200:
                print(f"⚠️  记忆评分请求失败: {response.status_code}")
                # 失败时返回默认分数
                return [{"content": t, "importance": 5} for t in texts]

            data = response.json()
            text = _extract_response_content(data)

            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            try:
                memories = json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        memories = json.loads(match.group())
                    except json.JSONDecodeError:
                        return [{"content": t, "importance": 5} for t in texts]
                else:
                    return [{"content": t, "importance": 5} for t in texts]

            if not isinstance(memories, list):
                return [{"content": t, "importance": 5} for t in texts]

            valid = []
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    valid.append({
                        "content": str(mem["content"]),
                        "importance": int(mem.get("importance", 5)),
                    })

            print(f"📝 为 {len(valid)} 条记忆完成自动评分")
            return valid

    except Exception as e:
        print(f"⚠️  记忆评分出错: {e}")
        return [{"content": t, "importance": 5} for t in texts]


# ============================================================
# P7 实体关系描述：读共享记忆，写一句话关系（复用记忆模型）
# ============================================================

def build_relation_description_prompt(pairs: List[Dict]) -> str:
    """构造批量实体对的关系描述 prompt。

    pairs 每项含 a_name / a_type / b_name / b_type / shared_total / evidence（共享记忆文本列表）。
    短名是长名子串的 pair 会被标记，提示模型重点审查同名异物。
    """
    blocks = []
    for i, pair in enumerate(pairs):
        evidence_lines = "".join(
            f"  · {str(text).strip()[:100]}\n" for text in (pair.get("evidence") or [])
        ).rstrip("\n")
        blocks.append(
            f'[{i}] "{pair.get("a_name")}"({pair.get("a_type", "other")}) ↔ '
            f'"{pair.get("b_name")}"({pair.get("b_type", "other")}) 共享{pair.get("shared_total", 0)}条记忆:\n'
            f"{evidence_lines}"
        )
    pair_blocks = "\n\n".join(blocks)
    suspicious = []
    for i, pair in enumerate(pairs):
        a = str(pair.get("a_name") or "")
        b = str(pair.get("b_name") or "")
        if len(a) <= 3 and len(b) > len(a) and b.find(a) >= 0:
            suspicious.append(str(i))
        elif len(b) <= 3 and len(a) > len(b) and a.find(b) >= 0:
            suspicious.append(str(i))
    suspicious_hint = (
        f"\n特别注意第 {', '.join(suspicious)} 对：一个名字可能是另一个名字的一部分（如「肉肉」 vs 「肉肉大米」），"
        f"务必先确认共享记忆里是否真的是同一个实体。"
        if suspicious else ""
    )
    return f"""以下实体对在我的记忆里共同出现过。对每对，先判断共享记忆里的名字是否真的指同一个实体
（警惕同名异物——短名字可能是更长名字的一部分）。{suspicious_hint}

确认是同一实体后，写一句话关系描述（≤30字，陈述事实，如"常去的地方"、"家人"、"最近一起去过的地方"）。
看不出实质关系、或判定为同名异物，填 null。

{pair_blocks}

只输出JSON数组: [{{"pair":0,"verify":"ok|namesake|unrelated","relation":"一句话描述或null"}}, ...]"""


async def describe_entity_relations(pairs: List[Dict]) -> Optional[dict]:
    """LLM 为一批候选实体对写一句话关系描述。

    返回 {pair_index: {"verify": "ok|namesake|unrelated", "relation": "..."}}；
    硬失败返回 None（上游故障，可稍后重试）。只把 verify=ok 且有 relation 的写回。
    模型复用记忆模型（ENTITY_RELATION_MODEL 空时 = MEMORY_MODEL）。
    失败时把具体原因写进模块级 RELATION_LAST_ERROR，供 Dashboard 展示。
    """
    global RELATION_LAST_ERROR
    RELATION_LAST_ERROR = ""
    if not pairs:
        return {}
    if not get_memory_api_key():
        RELATION_LAST_ERROR = "未配置记忆模型的 API Key"
        return None
    model = ENTITY_RELATION_MODEL or MEMORY_MODEL
    prompt = build_relation_description_prompt(pairs)
    request_messages = [{"role": "user", "content": prompt}]
    try:
        # 推理模型分析多对实体较慢，超时放宽到 180s，避免批量候选被掐断
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.1, "messages": request_messages},
            )
            if response.status_code != 200:
                RELATION_LAST_ERROR = f"HTTP {response.status_code}: {response.text[:200]}"
                print(f"⚠️ 关系描述请求失败: {response.status_code} {response.text[:200]}")
                return None
            text = _extract_response_content(response.json()).strip()
            try:
                parsed = parse_json_array(text)
            except ValueError as first_error:
                print(f"⚠️ 关系描述解析失败，正在重试: {first_error}")
                print(f"⚠️  原始文本前500字符: {text[:500]}")
                retry_response = await client.post(
                    get_memory_api_base_url(),
                    headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.1, "messages": request_messages + [
                        {"role": "assistant", "content": text[:2000]},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查这些实体对，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    RELATION_LAST_ERROR = f"重试 HTTP {retry_response.status_code}: {retry_response.text[:200]}"
                    print(f"⚠️ 关系描述重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                try:
                    parsed = parse_json_array(retry_text)
                except ValueError as retry_error:
                    RELATION_LAST_ERROR = f"模型两次返回都解析不出 JSON 数组：{retry_text[:200]}"
                    print(f"⚠️ 关系描述重试结果仍无法解析: {retry_error}")
                    print(f"⚠️  重试原始文本前500字符: {retry_text[:500]}")
                    return None
        result = {}
        for item in parsed if isinstance(parsed, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                pair_index = int(item.get("pair", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= pair_index < len(pairs):
                result[pair_index] = {
                    "verify": str(item.get("verify") or "unrelated").strip().lower(),
                    "relation": str(item.get("relation") or "").strip(),
                }
        return result
    except Exception as exc:
        RELATION_LAST_ERROR = f"调用异常：{exc}"
        print(f"⚠️ 关系描述调用异常: {exc}")
        return None
