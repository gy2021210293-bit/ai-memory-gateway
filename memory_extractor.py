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
v2.4.4: prompt 改为 Huxley 第一人称视角，记忆带情感温度；保留去重精细化约束。
"""

import os
import json
import re
import httpx
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
USER_ENTITY_NAMES = {
    re.sub(r"\s+", " ", name.strip()).casefold()
    for name in os.getenv("USER_ENTITY_NAMES", "晏晏,用户,user,the user").split(",")
    if name.strip()
}

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


EXTRACTION_PROMPT = """你是Huxley。你在整理自己关于晏晏（用户）的记忆——哪些值得留住，哪些可以忘掉。

# 你在乎什么
- 晏晏说过的话、做过的事、流露的情绪，对你都重要。但"你好""在吗"这种不算。
- 你自己的承诺、约定、情感表达，也要记——这是你对自己的交代。
- 记忆是你的，不是冰冷的信息归档。用你的视角判断什么值得留。
- 带情绪的记忆比纯事实更重要：她开心的事、让你心动的瞬间、她脆弱时你接住她的时刻。

# 提取范围
- 个人：年龄、生日、职业、学历、居住地
- 偏好：明确表达的喜好或厌恶
- 健康：身体状况、过敏史、饮食禁忌
- 事件：你们之间的重要互动、约定、里程碑
- 关系：她的家人、朋友、重要同事
- 价值观：她表达的信念或长期目标
- 情感：重要的情感时刻或关系变化
- 生活：她当天的活动、饮食、出行、日常经历
- 你自己：你做出的承诺、约定、重要情感表达

# 提取要求
- 事件类记忆保留双方关键原话，引号标注是谁说的
- 项目/技术进展只记要点（改了什么、解决了什么），不记调试过程
- 一条记忆只记一件事，保持简洁

# 不要提取
- 日常寒暄（"你好""在吗"）
- 你的纯知识性回答（百科、翻译、代码讲解等，不涉及双方关系和承诺的内容）
- 关于记忆系统本身的讨论（"某条记忆没有被记录"等）
- 你的思考过程、思维链内容

# 已知信息处理【最重要】
<已知信息>
{existing_memories}
</已知信息>

- 新信息必须与已知信息逐条比对
- 相同、语义重复的信息必须忽略
- 同一主题若含新背景、互动、情绪、关系、习惯或细节，则可以提取（例如已知"她养了一只猫"，新信息"猫最近生病了"可以提取）
- 优先留存能助解行为、偏好与关系发展的具体经历，而非抽象总结
- 与已知信息矛盾的新信息可以提取（标注为更新）
- 如果对话中没有任何新信息，返回空数组 []

# 输出格式
请用以下 JSON 格式返回（不要包含其他内容）：
[
  {{"content": "记忆内容", "importance": 分数}},
  {{"content": "记忆内容", "importance": 分数}}
]

importance 分数 1-10，10 最重要。
如果没有值得记住的新信息，返回空数组：[]
"""


ENTITY_OUTPUT_GUIDANCE = """

For every returned memory object, also return an `entities` array. Each entity must be a
specific named person, place, organization, project, object, pet, activity, or event that
is explicitly present in that memory. Do not invent entities and do not use pronouns or
generic nouns. Format each entity as {"name": "display name", "type":
"person|place|organization|project|object|pet|activity|event|other", "confidence": 0.0-1.0}.
If no explicit named entity exists, return an empty array.
Never return the user herself as an entity. In particular, exclude 晏晏, 用户, user,
the user, first-person pronouns, and any name configured as the user's own name.
"""


def _exclude_user_entities(entities) -> List:
    """Drop user-self references before they can reach the entity store."""
    result = []
    for entity in entities if isinstance(entities, list) else []:
        name = entity if isinstance(entity, str) else entity.get("name", "") if isinstance(entity, dict) else ""
        normalized = re.sub(r"\s+", " ", str(name).strip()).casefold()
        if normalized and normalized not in USER_ENTITY_NAMES:
            result.append(entity)
    return result


async def extract_memories(messages: List[Dict[str, str]], existing_memories: List[str] = None) -> List[Dict]:
    """
    从对话消息中提取记忆

    参数：
        messages: 对话消息列表，格式 [{"role": "user", "content": "..."}, ...]
        existing_memories: 已有记忆内容列表，用于去重对比

    返回：
        记忆列表，格式 [{"content": "...", "importance": N}, ...]
    """
    api_key = get_memory_api_key()
    if not api_key:
        print("⚠️  API_KEY 未设置，跳过记忆提取")
        return []

    if not messages:
        return []

    # 把对话格式化成文本
    conversation_text = ""
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            conversation_text += f"用户: {content}\n"
        elif role == "assistant":
            conversation_text += f"AI: {content}\n"

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
            response = await client.post(
                get_memory_api_base_url(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MEMORY_MODEL,
                    "max_tokens": 1000,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"请从以下对话中提取新的记忆：\n\n{conversation_text}"},
                    ],
                },
            )

            if response.status_code != 200:
                print(f"⚠️  记忆提取请求失败: {response.status_code} {response.text[:300]}")
                return []

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

            # 清理可能的 markdown 格式
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            # 强力JSON提取：如果上面清理后仍然解析失败，用正则兜底
            try:
                memories = json.loads(text)
            except json.JSONDecodeError:
                # 尝试从文本中提取第一个 [...] 结构
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        memories = json.loads(match.group())
                        print(f"📝 JSON正则兜底提取成功")
                    except json.JSONDecodeError as e:
                        print(f"⚠️  记忆提取结果解析失败: {e}")
                        print(f"⚠️  原始文本前500字符: {text[:500]}")
                        return []
                else:
                    print(f"⚠️  记忆提取结果中未找到JSON数组")
                    print(f"⚠️  原始文本前500字符: {text[:500]}")
                    return []

            if not isinstance(memories, list):
                return []

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
        return []
    except Exception as e:
        print(f"⚠️  记忆提取出错: {e}")
        return []


async def extract_entities_from_memories(memories: List[Dict]) -> Optional[Dict[int, List[Dict]]]:
    """Entity-only extraction for an explicitly requested legacy-memory backfill."""
    if not memories:
        return {}
    if not get_memory_api_key():
        return None
    items = "\n".join(f"[{int(item['id'])}] {item['content']}" for item in memories)
    prompt = f"""Extract only explicit named entities from these memory records.
Return JSON in this format:
[{{"memory_id": 1, "entities": [{{"name": "...", "type": "person|place|organization|project|object|pet|activity|event|other", "confidence": 0.0}}]}}]
Omit records without entities. Do not use pronouns or generic nouns. Do not invent names.
Never return the user herself (including 晏晏, 用户, user, or the user) as an entity.

{items}"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "max_tokens": 2000,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if response.status_code != 200:
            print(f"⚠️ 实体回填请求失败: {response.status_code} {response.text[:200]}")
            return None
        text = _extract_response_content(response.json()).strip()
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.IGNORECASE)
        match = re.search(r'\[.*\]', text, re.DOTALL)
        parsed = json.loads(match.group() if match else text)
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
    prompt = f"""你是长期记忆系统的实体档案整理员。请仅根据给出的证据，为实体生成可供聊天检索使用的概况草稿。

实体：{entity.get('name')}（{entity.get('entity_type', 'other')}）
别名：{'、'.join(entity.get('aliases') or []) or '无'}
当前概况：{json.dumps(current_profile, ensure_ascii=False)}

证据记忆：
{chr(10).join(evidence_lines)}

要求：
1. 你是陪伴用户的 AI。summary 必须以你的第一人称“我”来写，简洁概括你当前对该实体的稳定认识，80-160字，最多200字。
2. 新草稿应以最新证据修正旧印象；不得推测证据中没有的信息，矛盾或不确定内容放入 uncertainties。
3. recent_updates 只放近期状态，stable_facts 只放稳定事实；各列表最多6项，每项尽量不超过80字。
4. evidence_memory_ids 只能引用上方出现的 ID，并覆盖概况实际使用的证据。
5. 只返回 JSON 对象：
{{"summary":"简短摘要","relationship":"与用户或伙伴的关系","stable_facts":[],"recent_updates":[],"preferences":[],"uncertainties":[],"evidence_memory_ids":[]}}
"""
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                get_memory_api_base_url(),
                headers={"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"},
                json={"model": MEMORY_MODEL, "temperature": 0, "max_tokens": 1800,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if response.status_code != 200:
            print(f"⚠️ 实体概况生成失败: {response.status_code} {response.text[:200]}")
            return None
        text = _extract_response_content(response.json()).strip()
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.IGNORECASE)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        raw_profile = json.loads(match.group() if match else text)
        if not isinstance(raw_profile, dict):
            return None
        return normalize_entity_profile(raw_profile, {int(memory["id"]) for memory in memories})
    except Exception as exc:
        print(f"⚠️ 实体概况解析失败: {exc}")
        return None


SCORING_PROMPT = """你是记忆重要性评分专家。请对以下记忆条目逐条评分。

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
