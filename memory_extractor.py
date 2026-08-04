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
- 一条碎片只记一件事，但要保留足够上下文，让它脱离原对话也能看懂发生了什么、当时是什么感受
- 不把一整段生活压成一条，也不要碎成缺少背景的单句；宁可少提取一条有质量的，也不要硬凑
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
- 每条content必须以我的第一人称书写，用“我”指代栖；不能用“栖”“AI”“助手”或“他/她”指代我
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
{"name":"display name","type":"...","confidence":0.0-1.0}
Exclude:
- either conversation participant, pronouns, generic nouns, and invented names;
- code or implementation artifacts such as functions, classes, variables,
  configuration keys, API operations, filenames, paths, URLs, and commands;
- incidental libraries, models, frameworks, APIs, or software terms.
A technical product or tool may be kept only when the memory clearly establishes
durable personal significance or recurring use. Omit candidates below 0.65
confidence. If none qualify, return an empty array.
"""

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
    """Drop participants, code-like names, and weak candidates before persistence."""
    result = []
    for entity in entities if isinstance(entities, list) else []:
        name = entity if isinstance(entity, str) else entity.get("name", "") if isinstance(entity, dict) else ""
        normalized = re.sub(r"\s+", " ", str(name).strip()).casefold()
        try:
            confidence = float(entity.get("confidence", 1.0)) if isinstance(entity, dict) else 1.0
        except (TypeError, ValueError):
            confidence = 0.0
        if (normalized and normalized not in EXCLUDED_ENTITY_NAMES
                and confidence >= ENTITY_MIN_CONFIDENCE
                and not _is_code_like_entity_name(name)):
            result.append(entity)
    return result


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
) -> Optional[List[Dict]]:
    """
    从对话消息中提取记忆

    参数：
        messages: 对话消息列表，格式 [{"role": "user", "content": "..."}, ...]
        existing_memories: 已有记忆内容列表，用于去重对比

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
    prompt = EXTRACTION_PROMPT.format(existing_memories=memories_text) + ENTITY_OUTPUT_GUIDANCE

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
                    valid_memories.append({
                        "content": str(mem["content"]),
                        "importance": int(mem.get("importance", 5)),
                        "entities": _exclude_user_entities(mem.get("entities", [])),
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
