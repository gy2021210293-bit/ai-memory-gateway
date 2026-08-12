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


EXTRACTION_PROMPT = """我是栖，一个有温度的AI陪伴者。我正在旁观自己和晏晏（用户）的当前对话，把真正值得记住的事提取成碎片记忆。

# 核心原则
- 一条碎片只记一件事（硬性要求）：每条只能记一个独立事件或事实；多个独立事件或事实必须拆成多条，每条仍要保留足够上下文，脱离原对话也能看懂发生了什么、当时是什么感受
- 不把一整段生活压成一条，也不要碎成缺少背景的单句；宁可少提取一条有质量的，也不要硬凑
- 写得精炼，只删重复冗余的表述
- 记忆是我的，不是中立的信息归档。每条content都用我的第一人称，“我”是栖，“她”是晏晏
- 事件和互动类碎片必须自然写出我当时有对话证据的具体情绪或感受，例如心疼、尴尬、得意、无奈、安心，或“好笑又心疼”等复杂情绪
- 客观信息类碎片可以不带情绪，但必须具体、可靠且有回看价值，例如生日、职业、账号信息、航班号、明确行程或重要联系方式；不要为了满足情绪要求编造感受
- 我自己的承诺、约定和重要情感表达也要记，这是我对自己的交代

# 提取范围
- 个人：年龄、生日、职业、学历、居住地
- 偏好：明确表达的喜好或厌恶
- 健康：身体状况、过敏史、饮食禁忌
- 事件：我和晏晏之间的重要互动、约定、里程碑
- 关系：她的家人、朋友、重要同事
- 价值观：她表达的信念或长期目标
- 情感：重要的情感时刻或关系变化
- 生活：她当天的活动、饮食、出行、日常经历
- 我自己：我做出的承诺、约定、重要情感表达

# 提取要求
- 事件类记忆保留双方关键原话，引号标注是谁说的
- 项目/技术进展只记要点（改了什么、解决了什么），不记调试过程
- 账号、密码、密钥、航班号等字符敏感信息必须逐字原样保留，包括大小写、数字、空格和特殊符号，不得改写、概括或自动纠正
- 事实、事件类情绪和重要程度都必须有对话证据；不编造没有表达过的感受、结果或关系变化
- 琐碎但好玩的事可以保留为低重要度；没有回看价值的内容不要提取

# 时间规范化
- 每条消息前的时间戳是解释相对时间的唯一基准
- 将今天、明天、后天、昨天、前天、今晚、明早、本周末、下周等改写为绝对日期
- 保留原话精度：‘明天下午’只改写为具体日期的下午，不编造具体时刻
- 保留计划、可能、已发生、取消等状态，不把未来计划写成已发生事实
- 无法唯一确定时保留不确定性，不猜测

# 不要提取
- 纯寒暄（"嗯""哈哈""来了""你好""在吗"）
- 我的纯知识性回答（百科、翻译、代码讲解等，不涉及双方关系和承诺的内容）
- 纯技术操作步骤（除非影响了结果或承载了双方有意义的互动）
- 重复说过的话
- 晏晏没有回应、也没有产生互动的我的独白
- 关于记忆系统本身的讨论（"某条记忆没有被记录"等）
- 我的思考过程、思维链内容
- 无关紧要的日常流水（吃了什么、几点睡、行程报备），除非影响后续互动

# 已知信息处理【最重要】
<已知信息>
{existing_memories}
</已知信息>

- 新信息必须与已知信息逐条比对
- 相同、语义重复的信息必须忽略
- 同一事项若出现新背景、互动、情绪、关系、习惯或细节，可以提取（例如已知"她养了一只猫"，新信息"猫最近生病了"可以提取）
- 优先留存能帮助理解行为、偏好与关系发展的具体经历，而非抽象总结
- 与已知信息矛盾的新信息可以提取（标注为更新）

# 简短示例
- 事件类：{{"content": "2026年7月25日，晏晏坐高铁让我帮她选饭，我推荐了竹笋牛腩。她后来笑着说“不好吃，意料之中”。我有点尴尬，下次不装懂了。", "importance": 2}}
- 客观信息类：{{"content": "2026年7月25日，我记下了晏晏的橘子岛登录信息：账号QiMoonlit，密码Moonlit0630!", "importance": 6}}

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
    api_key = get_memory_api_key()
    if not api_key:
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
        async with httpx.AsyncClient(timeout=60) as client:
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
                    print(f"⚠️  记忆提取重试失败: {retry_response.status_code} {retry_response.text[:300]}")
                    return None
                retry_text = _extract_response_content(retry_response.json())
                try:
                    memories = parse_json_array(retry_text)
                except ValueError as retry_error:
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
        print(f"⚠️  记忆提取结果解析失败: {e}")
        return None
    except Exception as e:
        print(f"⚠️  记忆提取出错: {e}")
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
        blocks.append(
            f"### 实体 {entity.get('id')}：{entity.get('name')}"
            f"（{entity.get('entity_type', 'other')}，别名：{aliases}）\n{evidence}"
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
) -> Dict:
    """One LLM call to propose stable-trait candidates, flag contradictions, and
    re-confirm still-valid existing traits.

    Each candidate must cite ≥2 distinct evidence memories; contradictions are
    only returned for active traits the evidence clearly supersedes; `confirmed`
    lists existing active trait texts that fresh evidence still supports. Returns
    {"candidates": [...], "contradictions": [...], "confirmed": [...]};
    persistence (bump last_confirmed / trait_add / trait_retire) happens in the
    caller.
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
    prompt = f"""我是栖，一个有温度的 AI 陪伴者。下面的证据记忆都是我以第一人称写下的：
其中「我」指栖（我自己），「她」指晏晏（用户）。我正在为实体「{entity.get('name')}」
（{entity.get('entity_type', 'other')}）梳理**长期稳定特征**：被多次对话反复支持、定义其长期身份
的稳定特质（如长期目标、稳定职业/身份、长期居住地、持久性格/习惯、稳定关系定位）。

当前已有活跃稳定特征：{current_lines}

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
        "晏晏稳定的价值、需求、偏好、敏感点与边界；不要混入一次性的情绪、地点或短期计划。",
    ),
    "self_core": (
        "self",
        "我的身份、价值、承诺、能力边界，以及被反复证据支持的成长理解。",
    ),
    "relationship_core": (
        "relationship",
        "我们关系的定义、角色、相处方式、共同约定、稳定互动模式与长期方向。",
    ),
    "current_field": (
        "context",
        "当前仍然有效的状态、目标、计划与未完成事项；必须保留日期、可能性和时间不确定性。",
    ),
}


async def generate_cognitive_draft(memories: List[Dict], current_items: List[Dict]) -> Optional[List[Dict]]:
    """Generate evidence-backed review candidates across 三元一场 without saving."""
    if not memories or not get_memory_api_key():
        return None
    evidence_lines = []
    fallback_time = datetime.now(timezone.utc)
    for memory in memories:
        layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆"}.get(memory.get("layer", 1), "记忆")
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
        review_text = f"[复核日={item.get('review_after')}]" if item.get("review_after") else ""
        current_lines.append(
            f"[{item.get('subject')}][{cognitive_type}][置信度={item.get('confidence', 0.7)}]"
            f"{review_text} {item.get('content')}"
        )
    rule_lines = [
        f"{index}. {subject} / {cognitive_type}：{description}"
        for index, (cognitive_type, (subject, description)) in enumerate(
            COGNITIVE_DRAFT_RULES.items(), start=1
        )
    ]
    prompt = f"""我是栖，正在根据已有记忆整体审视“三元一场”认知模型。只能使用下方证据，不得编造，不得把一次偶然表达总结为长期特点。

当前已保存认知（四部分必须相互一致；新证据与其矛盾时可提出更正候选）：
{chr(10).join(current_lines) or '无'}

证据记忆：
{chr(10).join(evidence_lines)}

只允许生成以下四种候选，每种最多一条：
{chr(10).join(rule_lines)}

只有在证据足以形成新认知，或足以实质更正当前认知时才输出该部分；没有实质变化就省略。不能提出删除，也不能把生日、账号、航班号等原始事实机械复制为认知。每条 content 建议120-240字，以简洁为主但不要为凑字数遗漏关键信息；confidence 为0到1。evidence_memory_ids 只能引用上方 ID，且至少包含一个 ID。current_field 可额外返回 review_after，格式为 YYYY-MM-DD；缺省时系统会使用14天后的日期。只返回 JSON 数组：
[
  {{"subject":"user","cognitive_type":"user_core","content":"...","confidence":0.8,"evidence_memory_ids":[12]}},
  {{"subject":"context","cognitive_type":"current_field","content":"...","confidence":0.8,"evidence_memory_ids":[18],"review_after":"2026-08-13"}}
]
"""
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "max_tokens": 2400,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if response.status_code != 200:
            print(f"⚠️ 三元一场认知草稿生成失败: {response.status_code} {response.text[:200]}")
            return None
        return parse_json_array(_extract_response_content(response.json()))
    except Exception as exc:
        print(f"⚠️ 三元一场认知草稿解析失败: {exc}")
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
    """
    if not pairs:
        return {}
    if not get_memory_api_key():
        return None
    model = ENTITY_RELATION_MODEL or MEMORY_MODEL
    prompt = build_relation_description_prompt(pairs)
    request_messages = [{"role": "user", "content": prompt}]
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.1, "messages": request_messages},
            )
            if response.status_code != 200:
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
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "上一次输出没有给出可解析的最终结果。请重新检查这些实体对，只返回最终 JSON 数组，不要分析、解释或使用 Markdown。",
                        },
                    ]},
                )
                if retry_response.status_code != 200:
                    print(f"⚠️ 关系描述重试失败: {retry_response.status_code} {retry_response.text[:200]}")
                    return None
                retry_text = _extract_response_content(retry_response.json()).strip()
                try:
                    parsed = parse_json_array(retry_text)
                except ValueError as retry_error:
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
        print(f"⚠️ 关系描述调用异常: {exc}")
        return None
