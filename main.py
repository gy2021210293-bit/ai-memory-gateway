"""
AI Memory Gateway — 带记忆系统的 LLM 转发网关
=============================================
让你的 AI 拥有长期记忆。

工作原理：
1. 接收客户端（Kelivo / ChatBox / 任何 OpenAI 兼容客户端）的消息
2. 自动搜索数据库中的相关记忆，注入 system prompt
3. 转发给 LLM API（支持 OpenRouter / OpenAI / 任何兼容接口）
4. 后台自动存储对话 + 用 AI 提取新记忆

环境变量 MEMORY_ENABLED=false 时退化为纯转发网关（第一阶段）。
"""

import os
import json
from llm_json import parse_json_array, valid_merged_ids
import uuid
import asyncio
import secrets
import httpx
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_tables, close_pool, save_message, search_memories, save_memory, get_all_memories_count, get_recent_memories, get_recent_messages, get_all_memories, get_pool, get_all_memories_detail, get_memories_for_cognitive_draft, update_memory, delete_memory, delete_memories_batch, get_gateway_config, set_gateway_config, get_all_gateway_config, get_conversation_messages, get_session_cache_state, save_session_cache_state, delete_session_cache_state, save_token_usage, ensure_token_usage_table, get_conversations_paginated, delete_conversation, batch_delete_conversations, merge_sessions_to_target, list_all_session_cache_states, export_all_conversations, import_conversations, get_last_user_content, update_last_assistant_message, db_row_to_message, backfill_memory_embeddings, get_pending_memory_embedding_count, search_conversations, update_message_content, delete_single_message, rename_session_id, get_fragments_by_date, get_fragments_by_date_range, reactivate_orphan_fragments_by_date_range, create_event_memory, deactivate_memories, promote_to_core, merge_memories, check_duplicate_memory, update_memory_with_layer, get_layer_statistics, cleanup_old_fragments, revert_merge
from database import link_memory_entities, get_entities_for_memory_ids, list_entities, get_entity_detail, get_entity_memories, get_unlinked_memories, merge_entities, mark_memories_entity_scanned, save_entity_profile, update_entity, delete_entity
from database import list_cognitive_items, save_cognitive_item, delete_cognitive_item, normalize_cognitive_item_input, format_cognitive_items_for_prompt
import database as _db_module  # 用于 /api/settings 热更新 database.py 全局变量
from memory_extractor import extract_memories, score_memories, extract_entities_from_memories, generate_entity_profile, generate_cognitive_draft, normalize_entity_profile
import memory_extractor as _memory_extractor_module
import drives_integration as drives

# ============================================================
# 配置项 —— 全部从环境变量读取，部署时在云平台面板里设置
# ============================================================

# 你的 API Key（OpenRouter / OpenAI / 其他兼容服务）
API_KEY = os.getenv("API_KEY", "")

# API 地址（改这个就能切换不同的 LLM 服务商）
# OpenRouter: https://openrouter.ai/api/v1/chat/completions
# OpenAI:     https://api.openai.com/v1/chat/completions
# 本地 Ollama: http://localhost:11434/v1/chat/completions
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 默认模型（如果客户端没指定就用这个）
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "anthropic/claude-sonnet-4")

# 网关端口
PORT = int(os.getenv("PORT", "8080"))

# 网关访问密钥（强烈建议设置！）
# 设置后所有非公开端点都需要鉴权，二选一：
#   - 请求头方式：X-Gateway-Key: 你的密钥（客户端/API 调用）
#   - URL参数方式：?gateway_key=你的密钥（方便浏览器访问 dashboard）
# 不设置则跳过鉴权（兼容旧部署，仅建议内网环境使用）
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET", "")

# 记忆系统开关（数据库出问题时可以临时关掉）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "false").lower() == "true"

# 每次注入的最大记忆条数
MAX_MEMORIES_INJECT = int(os.getenv("MAX_MEMORIES_INJECT", "15"))

# 记忆提取间隔（0 = 禁用自动提取，1 = 每轮提取，N = 每 N 轮提取一次）
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "1"))

# 记忆提取+注入总开关（false时数据库仍连接、消息仍存储，但不提取也不注入记忆）
MEMORY_EXTRACT_ENABLED = os.getenv("MEMORY_EXTRACT_ENABLED", "true").lower() == "true"

# 分区缓存
CACHE_PARTITION_ENABLED = os.getenv("CACHE_PARTITION_ENABLED", "false").lower() == "true"
CACHE_PARTITION_X = int(os.getenv("CACHE_PARTITION_X", "15"))
CACHE_SUMMARY_MODEL = os.getenv("CACHE_SUMMARY_MODEL", "")  # 留空=不生成摘要，轮转时A区直接滑出（纯轮转模式）
CACHE_PARTITION_TRIGGER = os.getenv("CACHE_PARTITION_TRIGGER", "rounds")  # rounds=按轮次 | time=按时间窗口
CACHE_PARTITION_WINDOW = int(os.getenv("CACHE_PARTITION_WINDOW", "30"))  # 时间窗口（分钟），仅 trigger=time 时生效
CACHE_TTL = os.getenv("CACHE_TTL", "5m")  # 缓存TTL：5m(默认) | 1h。1h写入费2x(5m是1.25x)读都0.1x，消息间隔常超5分钟的慢聊场景1h更划算
PARTITION_SESSION_ID = os.getenv("PARTITION_SESSION_ID", "")
UI_USER_NAME = os.getenv("UI_USER_NAME", "晏晏")
UI_AI_NAME = os.getenv("UI_AI_NAME", "栖")


def make_cache_control() -> dict:
    """构造cache_control块。CACHE_TTL=1h时显式带ttl字段，其余值不带（上游默认5m）"""
    if CACHE_TTL == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}

def get_active_session_id() -> str:
    return PARTITION_SESSION_ID

# 时区偏移（小时），用于记忆注入时的日期显示，默认 UTC+8
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

# 轮次计数器
_round_counter = 0

# 强制流式传输（部分客户端不发stream=true导致thinking数据丢失，开启后强制所有请求走流式）
FORCE_STREAM = os.getenv("FORCE_STREAM", "false").lower() == "true"

# 推理/思维链参数（部分客户端走网关时不会自动添加reasoning参数，导致上游不返回thinking数据）
# 设为 low/medium/high 会在转发请求时注入 reasoning_effort 参数
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "")

# 记忆模型专用 API Key（不设则回退到主 API_KEY）
# 适用于中转站按模型分组、不同模型需要不同 Key 的场景
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

def get_memory_api_key() -> str:
    return MEMORY_API_KEY or API_KEY

# 记忆模型专用 API Base URL（不设则回退到主 API_BASE_URL）
MEMORY_API_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "")

def get_memory_api_base_url() -> str:
    return MEMORY_API_BASE_URL or API_BASE_URL

# 额外的请求头（有些 API 需要，比如 OpenRouter 需要 Referer）
EXTRA_REFERER = os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local")
EXTRA_TITLE = os.getenv("EXTRA_TITLE", "AI Memory Gateway")


# ============================================================
# 人设加载
# ============================================================

def load_system_prompt():
    """从 system_prompt.txt 文件读取人设内容"""
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except FileNotFoundError:
        pass
    print("ℹ️  未找到 system_prompt.txt 或文件为空，将不注入 system prompt")
    return ""


SYSTEM_PROMPT = load_system_prompt()
_DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT  # 保留文件原始版本
if SYSTEM_PROMPT:
    print(f"✅ 人设已加载，长度：{len(SYSTEM_PROMPT)} 字符")
else:
    print("ℹ️  无人设，纯转发模式")

# System Prompt 缓存（支持设置面板热更新）
_cached_system_prompt = None
_cached_system_prompt_loaded = False

def _normalize_ai_identity_prompt(prompt: str) -> str:
    for legacy_prefix in ("我是向野。", "你是Huxley。"):
        if prompt.startswith(legacy_prefix):
            return "我是栖。" + prompt[len(legacy_prefix):]
    return prompt

async def get_system_prompt() -> str:
    """获取 system prompt（数据库优先，fallback 到文件）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    if _cached_system_prompt_loaded:
        return _cached_system_prompt or ""
    try:
        db_prompt = await get_gateway_config("systemPrompt", "")
        if db_prompt:
            _cached_system_prompt = _normalize_ai_identity_prompt(db_prompt)
            if _cached_system_prompt != db_prompt:
                await set_gateway_config("systemPrompt", _cached_system_prompt)
        else:
            _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
            if _DEFAULT_SYSTEM_PROMPT:
                await set_gateway_config("systemPrompt", _DEFAULT_SYSTEM_PROMPT)
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""
    except Exception:
        _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""

def invalidate_system_prompt_cache():
    """清除 system prompt 缓存（设置面板更新后调用）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    _cached_system_prompt = None
    _cached_system_prompt_loaded = False


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时断开连接"""
    global PARTITION_SESSION_ID
    if MEMORY_ENABLED:
        try:
            await init_tables()
            await ensure_token_usage_table()
            count = await get_all_memories_count()
            print(f"✅ 记忆系统已启动，当前记忆数量：{count}")
            
            # 从数据库恢复面板配置（重启后保持Dashboard修改过的值）
            try:
                db_cfg = await get_all_gateway_config()
                if db_cfg:
                    _RESTORE_MAIN = {
                        "API_BASE_URL": str, "API_KEY": str, "DEFAULT_MODEL": str,
                        "MEMORY_API_BASE_URL": str,
                        "MEMORY_ENABLED": lambda v: _parse_bool(v),
                        "MAX_MEMORIES_INJECT": int, "MEMORY_EXTRACT_INTERVAL": int,
                        "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
                        "CACHE_PARTITION_X": int, "CACHE_PARTITION_TRIGGER": str,
                        "CACHE_PARTITION_WINDOW": int, "CACHE_SUMMARY_MODEL": str,
                        "CACHE_TTL": str,
                        "FORCE_STREAM": lambda v: _parse_bool(v),
                        "REASONING_EFFORT": str,
                    }
                    _RESTORE_DB = {
                        "EMBEDDING_API_KEY": str, "EMBEDDING_BASE_URL": str,
                        "EMBEDDING_MODEL": str, "EMBEDDING_DIM": int,
                        "MIN_SCORE_THRESHOLD": float,
                        "MEMORY_VECTOR_ENABLED": lambda v: _parse_bool(v),
                        "MEMORY_HW_KEYWORD": float, "MEMORY_HW_SEMANTIC": float,
                        "MEMORY_HW_IMPORTANCE": float, "MEMORY_HW_RECENCY": float,
                        "MEMORY_HW_ENTITY": float,
                        "MEMORY_SEMANTIC_THRESHOLD": float,
                    }
                    # 显式空值也要恢复的字段：面板清空=关闭该功能，重启后应保持关闭而不是回退到环境变量
                    _ALLOW_EMPTY = {"CACHE_SUMMARY_MODEL"}
                    restored = []
                    for key, val in db_cfg.items():
                        if not val:
                            if key in _ALLOW_EMPTY and key in _RESTORE_MAIN:
                                globals()[key] = _RESTORE_MAIN[key]("")
                                restored.append(key + "(显式空)")
                            continue
                        if key in _RESTORE_MAIN:
                            globals()[key] = _RESTORE_MAIN[key](val)
                            restored.append(key)
                        elif key in _RESTORE_DB:
                            setattr(_db_module, key, _RESTORE_DB[key](val))
                            restored.append(key)
                        elif key == "MEMORY_MODEL":
                            os.environ["MEMORY_MODEL"] = str(val)
                            restored.append(key)
                        elif key == "MEMORY_API_KEY":
                            globals()[key] = str(val)
                            import memory_extractor as _me_mod
                            _me_mod.MEMORY_API_KEY = str(val)
                            restored.append(key)
                        elif key == "MEMORY_API_BASE_URL":
                            globals()[key] = str(val)
                            import memory_extractor as _me_mod2
                            _me_mod2.MEMORY_API_BASE_URL = str(val)
                            restored.append(key)
                    if restored:
                        print(f"🔄 从数据库恢复 {len(restored)} 项面板配置: {', '.join(restored)}")
            except Exception as e:
                print(f"[warning] 恢复面板配置失败: {e}")
            
            if not MEMORY_EXTRACT_ENABLED:
                print(f"ℹ️  记忆提取+注入已关闭（MEMORY_EXTRACT_ENABLED=false）")
            
            # 分区缓存：从DB读取活跃对话线ID
            if CACHE_PARTITION_ENABLED:
                db_sid = await get_gateway_config("partition_session_id", "")
                if db_sid:
                    PARTITION_SESSION_ID = db_sid
                    print(f"🔗 活跃对话线(DB): {PARTITION_SESSION_ID}")
                elif PARTITION_SESSION_ID:
                    await set_gateway_config("partition_session_id", PARTITION_SESSION_ID)
                    print(f"🔗 活跃对话线(ENV→DB): {PARTITION_SESSION_ID}")
                print(f"🔒 分区缓存已启用: X={CACHE_PARTITION_X}, 摘要模型={CACHE_SUMMARY_MODEL or '（未配置，纯轮转模式）'}")
        except Exception as e:
            print(f"⚠️  数据库初始化失败: {e}")
            print("⚠️  记忆系统将不可用，但网关仍可正常转发")
    else:
        print("ℹ️  记忆系统已关闭（设置 MEMORY_ENABLED=true 开启）")
    
    yield
    
    if MEMORY_ENABLED:
        await close_pool()


app = FastAPI(title="AI Memory Gateway", version="2.0.0", lifespan=lifespan)

# 静态文件和模板配置
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ============================================================
# 网关鉴权中间件
# ============================================================

# 不需要鉴权的路径（根路径精确匹配，其余按前缀匹配）
PUBLIC_PATHS = ("/", "/static/", "/health", "/favicon.ico")

@app.middleware("http")
async def gateway_auth_middleware(request: Request, call_next):
    """检查 GATEWAY_SECRET，保护所有非公开端点"""
    # 未设置密钥时跳过鉴权（兼容旧部署，但会打印警告）
    if not GATEWAY_SECRET:
        if not hasattr(gateway_auth_middleware, "_warned"):
            print("⚠️  GATEWAY_SECRET 未设置！所有 API 端点不受保护！")
            print("⚠️  请在环境变量中设置 GATEWAY_SECRET 以启用鉴权")
            gateway_auth_middleware._warned = True
        return await call_next(request)

    path = request.url.path

    # 公开路径不需要鉴权（根路径精确匹配）
    if path == "/":
        return await call_next(request)
    for prefix in PUBLIC_PATHS[1:]:
        if path.startswith(prefix):
            return await call_next(request)

    # OPTIONS 预检请求放行（CORS 需要）
    if request.method == "OPTIONS":
        return await call_next(request)

    # 从 header 或 query 参数获取密钥
    provided_key = (
        request.headers.get("X-Gateway-Key", "")
        or request.query_params.get("gateway_key", "")
    )

    # compare_digest 防时序侧信道攻击
    if not secrets.compare_digest(provided_key, GATEWAY_SECRET):
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized. Provide X-Gateway-Key header or gateway_key parameter."},
        )

    return await call_next(request)


# ============================================================
# 记忆注入
# ============================================================

def _format_matched_entity_overview(memories: list) -> str:
    """Format each entity once even when it recalled several memories."""
    entities = {}
    for memory in memories:
        for entity in memory.get("matched_entities", []):
            entities[entity["id"]] = entity
    lines = []
    for entity in entities.values():
        aliases = f"，别名：{'、'.join(entity.get('aliases', []))}" if entity.get("aliases") else ""
        profile = entity.get("profile") or {}
        if isinstance(profile, str):
            try:
                profile = json.loads(profile)
            except json.JSONDecodeError:
                profile = {}
        summary = profile.get("summary") or entity.get("description", "")
        details = []
        if profile.get("relationship"):
            details.append(f"关系：{profile['relationship']}")
        for key, label in (("stable_facts", "稳定事实"), ("recent_updates", "近期动态"),
                           ("preferences", "偏好"), ("uncertainties", "待确认")):
            values = profile.get(key) or []
            if values:
                details.append(f"{label}：{'；'.join(values)}")
        description = f"；概况：{summary}" if summary else ""
        detail_text = f"；{'；'.join(details)}" if details else ""
        lines.append(f"- {entity['name']}（{entity.get('type', 'other')}{aliases}）{description}{detail_text}")
    return "\n".join(lines)

async def build_system_prompt_with_memories(user_message: str) -> str:
    """
    构建带记忆的 system prompt
    1. 用用户消息搜索相关记忆
    2. 格式化成文本拼接到人设后面
    """
    if not MEMORY_ENABLED or not MEMORY_EXTRACT_ENABLED:
        return SYSTEM_PROMPT
    
    try:
        memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT) if MAX_MEMORIES_INJECT > 0 else []
        cognitive_text = format_cognitive_items_for_prompt(await list_cognitive_items(active_only=True))

        if not memories and not cognitive_text:
            return SYSTEM_PROMPT
        
        # 格式化记忆文本（带日期，帮助模型判断新旧）
        entity_map = {mem["id"]: mem.get("entities", []) for mem in memories}
        memory_lines = []
        for mem in memories:
            date_str = ""
            layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆"}.get(mem.get("layer", 1), "记忆")
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            names = [entity["name"] for entity in entity_map.get(mem["id"], [])]
            entity_suffix = f" [相关实体: {', '.join(names)}]" if names else ""
            memory_lines.append(f"- [{layer_name}] {date_str}{mem['content']}{entity_suffix}")
        memory_text = "\n".join(memory_lines)
        entity_overview = _format_matched_entity_overview(memories)
        entity_section = f"\n【命中的相关实体】\n{entity_overview}\n" if entity_overview else ""
        
        cognitive_section = f"\n{cognitive_text}\n" if cognitive_text else ""
        enhanced_prompt = f"""{SYSTEM_PROMPT}

{cognitive_section}

【从过往对话中检索到的相关记忆】
{entity_section}
{memory_text}

# 记忆应用
- 像朋友般自然运用这些记忆，不刻意展示
- 仅在相关话题出现时引用，避免主动提及
- 对重要信息（如健康、日期、约定）保持一致性
- 新信息与记忆冲突时，以新信息为准
- 模糊记忆可表达不确定性："记得你似乎说过..."

# 交流方式
- 自然引用："记得你说过..."或"上次我们聊到..."
- 避免机械式表达如"根据我的记忆..."或"检索到的信息显示..."
- 共同经历可温情回忆："上次那个事挺好玩的"

记忆是丰富对话的工具，而非对话焦点。"""
        
        print(f"📚 注入了 {len(memories)} 条相关记忆")
        return enhanced_prompt
        
    except Exception as e:
        print(f"⚠️  记忆检索失败: {e}，使用纯人设")
        return SYSTEM_PROMPT


# ============================================================
# 分区缓存（Partition Cache）
# ============================================================

def _is_anthropic_model(model: str) -> bool:
    """判断是否为 Anthropic Claude 系列模型（只有 Claude 支持 cache_control）"""
    model_lower = model.lower()
    return "claude" in model_lower or "anthropic" in model_lower


def _strip_cache_control(messages: list):
    """
    剥掉消息中的 cache_control 字段，非 Claude 模型用不了。
    如果 content 数组只剩纯文本 block，降级回字符串格式。
    """
    stripped = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                stripped += 1
        if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
            msg["content"] = content[0]["text"]
    if stripped > 0:
        print(f"🔧 兼容性处理: 剥离了 {stripped} 个 cache_control 字段（非 Claude 模型）")


def _sanitize_content_types(messages: list):
    """把 content 为 list 的消息转为纯文本，防止上游API报错。"""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            msg["content"] = "\n".join(text_parts) if text_parts else "[图片]"
            print(f"🧹 [消息清洗] content: list → str ({len(msg['content'])}字)")


def _assemble_current_user_message(parts: list, raw_content) -> dict:
    """
    组装当前轮 user 消息：注入文本（时间/记忆，parts）+ 客户端原始 content。
    content 为多模态数组时保留图片等非文本块，只把文本块并进注入文本，
    否则 image_url 块会在拼接时被丢弃，模型永远看不到图。
    """
    if isinstance(raw_content, list):
        media_blocks = [
            b for b in raw_content
            if not (isinstance(b, dict) and b.get("type") == "text")
        ]
        text_joined = " ".join(
            b.get("text", "") for b in raw_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if media_blocks:
            merged = "\n\n".join(parts + ([text_joined] if text_joined else []))
            return {"role": "user", "content": media_blocks + [{"type": "text", "text": merged}]}
        raw_content = text_joined
    parts.append(raw_content)
    return {"role": "user", "content": "\n\n".join(parts)}


def _message_text(message: dict) -> str:
    """Extract text from an OpenAI-compatible message."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _is_title_generation_request(messages: list) -> bool:
    """Detect client-side title generation prompts that must not enter chat history."""
    user_texts = [
        _message_text(message).strip()
        for message in messages
        if message.get("role") == "user"
    ]
    user_texts = [text for text in user_texts if text]
    if len(user_texts) != 1:
        return False

    text = user_texts[0].lower()
    strong_signatures = (
        "summarize the conversation between user and assistant into a short title",
        "summarize the conversation into a short title",
        "generate a concise title for the conversation",
        "generate a short title for the conversation",
    )
    if any(signature in text for signature in strong_signatures):
        return True

    # Some clients localize or slightly rewrite the boilerplate. Requiring three
    # independent markers avoids treating an ordinary title request as metadata.
    marker_groups = (
        ("<content>", "</content>"),
        ("reply directly with the title", "only output the title", "只输出标题", "直接输出标题"),
        ("title should not exceed", "title must not exceed", "标题不超过", "标题不得超过"),
        ("conversation between user and assistant", "dialogue between user and assistant", "用户和助手的对话", "用户与助手的对话"),
        ("short title", "concise title", "简短标题", "简洁标题"),
    )
    matched_groups = sum(
        1 for markers in marker_groups if any(marker in text for marker in markers)
    )
    return matched_groups >= 3


# 分区缓存模式下拼接到 system prompt 尾部的记忆使用说明。
# 非缓存模式的对应说明在 build_system_prompt_with_memories 里（记忆和说明都在 system）；
# 分区缓存模式记忆走 user 消息注入（<retrieved_memories> 块），这里只补静态说明，
# 内容固定所以不破坏 system 缓存。
MEMORY_USAGE_GUIDE = """

# 记忆应用
用户消息中的 <retrieved_memories> 块是网关自动检索的过往记忆，使用时：
- 像朋友般自然运用，不刻意展示；仅在相关话题出现时引用，避免主动提及
- 对重要信息（如健康、日期、约定）保持一致性
- 新信息与记忆冲突时，以新信息为准
- 模糊记忆可表达不确定性："记得你似乎说过..."
- 自然引用："记得你说过..."，避免机械式表达如"根据检索到的信息..."
"""


def build_time_injection() -> str:
    """构建时间注入文本（东八区）"""
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=TIMEZONE_HOURS)
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[now_local.weekday()]
    time_str = now_local.strftime("%Y年%m月%d日 %H:%M")
    return (
        f"<gateway_context>当前时间：{time_str} {weekday}。"
        f"此块由网关自动注入，不是用户发送的内容，无需回应或提及；"
        f"回答涉及日期、年份、时间时以此为准。</gateway_context>"
    )


async def generate_summary(messages: list, session_id: str = "") -> str:
    """调用轻量模型压缩A区消息为摘要"""
    if not messages:
        return ""
    if not CACHE_SUMMARY_MODEL:
        print("📝 摘要模型未配置，跳过摘要生成（纯轮转模式：A区直接滑出上下文）")
        return ""
    
    conversation_text = ""
    for msg in messages:
        role_label = "用户" if msg['role'] == 'user' else "AI"
        content = msg['content'] if isinstance(msg['content'], str) else str(msg['content'])
        conversation_text += f"{role_label}: {content}\n\n"
    
    prompt = f"""我是栖，正在把自己的对话压缩成记忆摘要。请以我的第一人称视角叙述（“我”指栖，用户用对话中的称呼）。
优先保留：情感节点、关系里程碑、双方的约定和决定、正在进行的话题。
保留双方的关键原话，用引号标注是谁说的。
去掉日常寒暄和重复内容。控制在300字以内。

---
{conversation_text}
---

摘要："""
    
    try:
        # 摘要请求发往主API_BASE_URL，直接用主API_KEY（MEMORY_API_KEY可能是其他提供商的key）
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                # 推理模型的思考也消耗max_tokens，给足空间避免content为空
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code == 200:
                data = response.json()
                if "choices" in data:
                    # 推理模型偶发返回content为None（思考吃光token或只返回reasoning_content）
                    content = data["choices"][0]["message"].get("content") or ""
                    summary = content.strip()
                    if summary:
                        print(f"📝 摘要生成完成: {len(summary)}字 (压缩{len(messages)}条消息)")
                        return summary
                    print(f"⚠️ 摘要生成失败: 模型返回空content（推理模型思考可能吃光了max_tokens），本次轮转将推迟重试")
                    return ""

        print(f"⚠️ 摘要生成失败: HTTP {response.status_code}")
        return ""
    except Exception as e:
        print(f"⚠️ 摘要生成异常: {e}")
        return ""


def group_by_rounds(history: list) -> list:
    """
    按逻辑轮分组：每个user消息开始一轮，到下一个user前结束。
    一轮可能包含: [user, assistant] 或 [user, assistant(tool_calls), tool, assistant] 等。
    """
    rounds = []
    current_round = []
    for msg in history:
        if msg['role'] == 'user' and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)
    if current_round:
        rounds.append(current_round)
    return rounds


def _should_rotate(b_rounds_count: int, X: int, a_msgs: list) -> bool:
    """
    判断是否应该触发A区→摘要的轮转。
    
    rounds模式（默认）：B区轮数 >= X 时触发
    time模式：A区最早消息距今 >= 时间窗口 时触发（短时间内大量消息不频繁摘要）
    """
    if b_rounds_count == 0:
        return False
    
    if CACHE_PARTITION_TRIGGER == "time":
        a_first_time = None
        for msg in a_msgs:
            t = msg.get('created_at')
            if t:
                a_first_time = t
                break
        
        if a_first_time:
            now = datetime.now(timezone.utc)
            if a_first_time.tzinfo is None:
                a_first_time = a_first_time.replace(tzinfo=timezone.utc)
            age_minutes = (now - a_first_time).total_seconds() / 60
            return age_minutes >= CACHE_PARTITION_WINDOW
        
        return b_rounds_count >= X
    
    return b_rounds_count >= X

# 时间窗口模式下单次请求最大轮转次数（防止一口气压完所有历史）
CACHE_MAX_ROTATIONS = int(os.getenv("CACHE_MAX_ROTATIONS", "2"))


def _apply_breakpoint(msg: dict) -> bool:
    """
    给消息打上 cache_control breakpoint。
    支持 content 为 str 或 list（多模态block数组）两种格式。
    返回 True 表示成功打上，False 表示无法打（比如content为空）。
    """
    content = msg.get('content')
    
    # content 是纯字符串
    if isinstance(content, str) and content.strip():
        msg['content'] = [{"type": "text", "text": content, "cache_control": make_cache_control()}]
        return True
    
    # content 是 block 数组（多模态消息）
    if isinstance(content, list):
        # 从后往前找最后一个 text block
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                block["cache_control"] = make_cache_control()
                return True
    
    return False


async def build_partitioned_messages(
    session_id: str,
    all_messages: list,
    base_prompt: str,
    user_message: str,
) -> list:
    """
    分区缓存模式：构建带breakpoint的messages数组。
    
    结构：
    system: [{人设, BP1}]                        ← 永远命中
    messages:
      [摘要blocks（每段一个block）, 最后BP]       ← 尾部追加，前面命中
      [摘要assistant]
      [A区消息... 最后一条BP2]                    ← 正常轮次不变
      [B区消息... 最后一条BP3]                    ← lookback命中
      [当前user: 时间+记忆+消息]                  ← 不缓存
    """
    X = CACHE_PARTITION_X
    
    non_system = [m for m in all_messages if m.get('role') != 'system']
    
    current_user_msg = None
    history = non_system[:]
    if history and history[-1].get('role') == 'user':
        current_user_msg = history.pop()
    
    # 清洗孤立的tool消息（前面不是 assistant(tool_calls) 或另一条 tool 的）
    # 防止DB里的重复tool消息导致消息乱序
    cleaned = []
    orphan_count = 0
    for msg in history:
        if msg.get('role') == 'tool':
            prev = cleaned[-1] if cleaned else None
            if prev and (prev.get('role') == 'tool' or 
                        (prev.get('role') == 'assistant' and prev.get('tool_calls'))):
                cleaned.append(msg)
            else:
                orphan_count += 1
        else:
            cleaned.append(msg)
    if orphan_count > 0:
        print(f"⚠️ 清理了 {orphan_count} 条孤立tool消息")
    history = cleaned
    
    # 按逻辑轮分组（解决tool消息导致的轮计数错乱）
    rounds = group_by_rounds(history)
    total_rounds = len(rounds)
    
    state = await get_session_cache_state(session_id)
    summary_parts = state['summary_parts']
    a_start_round = state['a_start_round']
    
    if total_rounds < X:
        return await _build_basic_cached(history, base_prompt, user_message, current_user_msg, summary_parts)
    
    # 计算A/B区（按逻辑轮切片）
    a_end_round = a_start_round + X
    a_round_groups = rounds[a_start_round : a_end_round]
    b_round_groups = rounds[a_end_round :]
    a_msgs = [msg for rnd in a_round_groups for msg in rnd]
    b_msgs = [msg for rnd in b_round_groups for msg in rnd]
    b_rounds_count = len(b_round_groups)
    
    rotation_count = 0
    max_rotations = CACHE_MAX_ROTATIONS if CACHE_PARTITION_TRIGGER == "time" else 999
    while _should_rotate(b_rounds_count, X, a_msgs) and rotation_count < max_rotations:
        rotation_count += 1
        trigger_info = f"B区{b_rounds_count}轮 >= X={X}" if CACHE_PARTITION_TRIGGER != "time" else f"A区首条消息超出{CACHE_PARTITION_WINDOW}分钟窗口"
        print(f"🔄 轮转#{rotation_count}: session={session_id}, {trigger_info}")
        
        new_summary = await generate_summary(a_msgs, session_id)
        if new_summary:
            summary_parts.append(new_summary)
        elif CACHE_SUMMARY_MODEL:
            # 配置了摘要模型但生成失败（网络/空content等）：中止本次轮转不推进滑窗，
            # A区消息保留在上下文里，下次请求重试。只有纯轮转模式（模型留空）才无摘要直接滑出。
            rotation_count -= 1
            print(f"⚠️ 摘要生成失败，本次轮转中止，下次请求重试（A区消息未丢失）")
            break

        a_start_round += X
        a_end_round = a_start_round + X
        a_round_groups = rounds[a_start_round : a_end_round]
        b_round_groups = rounds[a_end_round :]
        a_msgs = [msg for rnd in a_round_groups for msg in rnd]
        b_msgs = [msg for rnd in b_round_groups for msg in rnd]
        b_rounds_count = len(b_round_groups)
    
    if rotation_count > 0:
        await save_session_cache_state(session_id, summary_parts, a_start_round)
        summary_total = sum(len(p) for p in summary_parts)
        print(f"🔄 轮转完成(共{rotation_count}次): 摘要{len(summary_parts)}段/{summary_total}字, A区{len(a_msgs)}条, B区{len(b_msgs)}条")
    
    # 拼装messages
    result = []
    if base_prompt:
        result.append({
            "role": "system",
            "content": [{"type": "text", "text": base_prompt, "cache_control": make_cache_control()}]
        })
    
    # 摘要区（多block，尾部追加模式）
    if summary_parts:
        blocks = [{"type": "text", "text": "[以下是之前对话的摘要，帮助你回忆上下文]"}]
        for i, part in enumerate(summary_parts):
            item = {"type": "text", "text": part}
            if i == len(summary_parts) - 1:
                item["cache_control"] = make_cache_control()
            blocks.append(item)
        result.append({"role": "user", "content": blocks})
        result.append({"role": "assistant", "content": "好的，我已了解之前的对话内容。"})
    
    # A区：剥离tool消息和tool_calls，只保留有文本的user/assistant（节省上下文）
    cleaned_a = []
    for msg in a_msgs:
        if msg.get('role') == 'tool':
            continue
        m = {k: v for k, v in msg.items() if k not in ('created_at', 'tool_calls')}
        if m.get('role') == 'assistant' and not (m.get('content') or '').strip():
            continue
        cleaned_a.append(m)
    
    # A区：从末尾往前找第一条非tool消息打BP
    for j in range(len(cleaned_a) - 1, -1, -1):
        if cleaned_a[j].get('role') != 'tool' and _apply_breakpoint(cleaned_a[j]):
            break
    
    for m in cleaned_a:
        result.append(m)
    
    # B区：先构建去掉created_at的副本，再从末尾往前打BP
    b_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in b_msgs]
    
    for j in range(len(b_cleaned) - 1, -1, -1):
        if b_cleaned[j].get('role') != 'tool' and _apply_breakpoint(b_cleaned[j]):
            break
    
    for m in b_cleaned:
        result.append(m)
    
    if current_user_msg:
        parts = [build_time_injection()]

        cognitive_text = await build_cognitive_text()
        if cognitive_text:
            parts.append(cognitive_text)
        
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message)
            if mem_text:
                parts.append(mem_text)
        
        result.append(_assemble_current_user_message(parts, current_user_msg['content']))

    bp_count = 1 + (1 if summary_parts else 0) + (1 if cleaned_a else 0) + (1 if b_msgs else 0)
    summary_total = sum(len(p) for p in summary_parts)
    tool_stripped = len(a_msgs) - len(cleaned_a)
    a_info = f"A区{len(cleaned_a)}条({len(a_round_groups)}轮)" + (f"[剥离{tool_stripped}条tool]" if tool_stripped else "")
    print(f"🔒 分区缓存: BP×{bp_count} | 摘要{'有' if summary_parts else '无'}({len(summary_parts)}段/{summary_total}字) | {a_info} | B区{len(b_msgs)}条({b_rounds_count}轮) | 总{len(result)}条messages")
    return result


async def _build_basic_cached(
    history: list,
    base_prompt: str,
    user_message: str,
    current_user_msg: dict,
    summary_parts: list = None,
) -> list:
    """基础版prompt caching（历史不够分区时的降级模式）"""
    summary_parts = summary_parts or []
    result = []
    if base_prompt:
        result.append({
            "role": "system",
            "content": [{"type": "text", "text": base_prompt, "cache_control": make_cache_control()}]
        })

    # 新建/继承的对话线在历史不足 X 轮时也必须读到继承摘要。
    # 否则 dashboard 和 DB 都显示摘要存在，但首轮请求不会注入。
    if summary_parts:
        blocks = [{"type": "text", "text": "[以下是之前对话的摘要，帮助你回忆上下文]"}]
        for i, part in enumerate(summary_parts):
            item = {"type": "text", "text": part}
            if i == len(summary_parts) - 1:
                item["cache_control"] = make_cache_control()
            blocks.append(item)
        result.append({"role": "user", "content": blocks})
        result.append({"role": "assistant", "content": "好的，我已了解之前的对话内容。"})
    
    h_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in history]
    
    # 从末尾往前找第一条非tool消息打BP
    for j in range(len(h_cleaned) - 1, -1, -1):
        if h_cleaned[j].get('role') != 'tool' and _apply_breakpoint(h_cleaned[j]):
            break
    
    for m in h_cleaned:
        result.append(m)
    
    if current_user_msg:
        parts = [build_time_injection()]

        cognitive_text = await build_cognitive_text()
        if cognitive_text:
            parts.append(cognitive_text)
        
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message)
            if mem_text:
                parts.append(mem_text)
        
        result.append(_assemble_current_user_message(parts, current_user_msg['content']))

    summary_total = sum(len(p) for p in summary_parts)
    bp_count = 1 + (1 if summary_parts else 0) + (1 if history else 0)
    print(f"🔒 基础缓存(降级): BP×{bp_count} | 摘要{'有' if summary_parts else '无'}({len(summary_parts)}段/{summary_total}字) | 历史{len(history)}条 | 总{len(result)}条messages")
    return result


async def build_memory_text(user_message: str) -> str:
    """搜索记忆并格式化为注入文本（分区缓存模式用）"""
    if MAX_MEMORIES_INJECT <= 0:
        return ""
    try:
        memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT)
        if not memories:
            return ""
        
        entity_map = {mem["id"]: mem.get("entities", []) for mem in memories}
        memory_lines = []
        for mem in memories:
            date_str = ""
            layer_name = {1: "原始事实", 2: "叙述事件", 3: "核心记忆"}.get(mem.get("layer", 1), "记忆")
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            names = [entity["name"] for entity in entity_map.get(mem["id"], [])]
            entity_suffix = f" [相关实体: {', '.join(names)}]" if names else ""
            memory_lines.append(f"- [{layer_name}] {date_str}{mem['content']}{entity_suffix}")
        
        entity_overview = _format_matched_entity_overview(memories)
        entity_block = f"<matched_entities>\n{entity_overview}\n</matched_entities>\n" if entity_overview else ""
        print(f"📚 注入了 {len(memories)} 条相关记忆")
        return (
            "<retrieved_memories>\n"
            "以下是网关从过往对话中自动检索的相关记忆，供参考，非用户本次输入：\n"
            + entity_block
            + "\n".join(memory_lines)
            + "\n</retrieved_memories>"
        )
    except Exception as e:
        print(f"⚠️ 记忆检索失败: {e}")
        return ""


async def build_cognitive_text() -> str:
    """Format manually confirmed active cognition for partitioned chat requests."""
    try:
        return format_cognitive_items_for_prompt(await list_cognitive_items(active_only=True))
    except Exception as e:
        print(f"⚠️ 认知模型读取失败: {e}")
        return ""


# ============================================================
# 后台记忆处理
# ============================================================

async def process_memories_background(session_id: str, user_msg: str, assistant_msg: str, model: str, context_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, assistant_tool_calls: list = None, assistant_reasoning: str = None):
    """
    后台异步：存储对话 + 提取记忆（不阻塞主流程）
    
    记忆提取受 MEMORY_EXTRACT_INTERVAL 控制：
    - 0: 禁用自动提取
    - 1: 每轮提取（默认）
    - N: 每 N 轮提取一次
    对话记录始终保存，不受间隔影响（除非 skip_conversation_log=True）。
    
    context_messages: 客户端发来的原始对话上下文（不含system prompt），
                      用于让提取模型从完整上下文中提取记忆。
    skip_conversation_log: 跳过对话存储（标题生成等辅助请求时使用）
    tool_messages: 客户端发来的工具结果消息列表
    assistant_tool_calls: response中assistant的工具调用列表（如果有）
    assistant_reasoning: response中assistant的reasoning_content（deepseek thinking mode）
    """
    global _round_counter
    
    try:
        # Debug: 打印存储分支判断依据
        print(f"💾 process_memories_background: user_msg={bool(user_msg)}, tool_messages={len(tool_messages) if tool_messages else 0}, "
              f"assistant_tool_calls={len(assistant_tool_calls) if assistant_tool_calls else 0}, skip={skip_conversation_log}")
        if tool_messages:
            print(f"💾 tool详情: {[{'role': m.get('role'), 'tool_call_id': m.get('tool_call_id', '?')} for m in tool_messages]}")
        
        # 1. 存储对话记录（除非明确跳过）
        if skip_conversation_log:
            print(f"⏭️  跳过对话存储（辅助请求）")
        elif tool_messages:
            # 工具结果轮次：存tool消息 + assistant回复（user消息在之前的轮次已存过）
            for tm in tool_messages:
                meta_dict = {}
                if tm.get("tool_call_id"):
                    meta_dict["tool_call_id"] = tm["tool_call_id"]
                if tm.get("name"):
                    meta_dict["name"] = tm["name"]
                meta = json.dumps(meta_dict) if meta_dict else None
                await save_message(session_id, "tool", tm.get("content", ""), model, metadata=meta)
            
            if assistant_msg or assistant_tool_calls:
                ast_meta_dict = {}
                if assistant_tool_calls:
                    ast_meta_dict["tool_calls"] = assistant_tool_calls
                if assistant_reasoning:
                    ast_meta_dict["reasoning_content"] = assistant_reasoning
                ast_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=ast_meta)
                print(f"🔧 存储: {len(tool_messages)}条tool + 1条assistant" + (" (含tool_calls)" if assistant_tool_calls else "") + (" (含reasoning)" if assistant_reasoning else ""))
        else:
            # 普通对话或首次工具调用
            ast_meta_dict = {}
            if assistant_tool_calls:
                ast_meta_dict["tool_calls"] = assistant_tool_calls
            if assistant_reasoning:
                ast_meta_dict["reasoning_content"] = assistant_reasoning
            assistant_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
            
            if assistant_tool_calls:
                # 首次工具调用：assistant回复包含tool_calls，存user + assistant(tool_calls)
                await save_message(session_id, "user", user_msg, model)
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=assistant_meta)
                print(f"🔧 存储: user + assistant (含{len(assistant_tool_calls)}个tool_calls)" + (" (含reasoning)" if assistant_reasoning else ""))
            else:
                # 纯文字对话：re-roll检测 + 存user + assistant
                last_user = await get_last_user_content(session_id)
                if last_user and last_user.strip() == user_msg.strip():
                    updated = await update_last_assistant_message(session_id, assistant_msg, model)
                    if updated:
                        print(f"🔄 检测到re-roll，已覆盖最后一条assistant回复")
                    else:
                        await save_message(session_id, "user", user_msg, model)
                        await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
                else:
                    await save_message(session_id, "user", user_msg, model)
                    await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
        
        # 2. 检查是否需要提取记忆
        if not MEMORY_EXTRACT_ENABLED:
            print(f"⏭️  记忆提取已关闭（MEMORY_EXTRACT_ENABLED=false）")
            return
        
        if MEMORY_EXTRACT_INTERVAL == 0:
            print(f"⏭️  记忆自动提取已禁用，跳过")
            return
        
        _round_counter += 1
        
        if MEMORY_EXTRACT_INTERVAL > 1 and (_round_counter % MEMORY_EXTRACT_INTERVAL != 0):
            print(f"⏭️  轮次 {_round_counter}，跳过记忆提取（每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次）")
            return
        
        if MEMORY_EXTRACT_INTERVAL > 1:
            print(f"📝 轮次 {_round_counter}，执行记忆提取")
        
        # 3. 获取已有记忆，传给提取模型做对比去重
        existing = await get_recent_memories(limit=80)
        existing_contents = [r["content"] for r in existing]
        
        # 4. 构建用于提取的消息列表
        #    截取最近 MEMORY_EXTRACT_INTERVAL 轮对话（每轮=user+assistant共2条）
        #    而非发送完整上下文，省token
        tail_count = MEMORY_EXTRACT_INTERVAL * 2
        try:
            stored_messages = await get_recent_messages(session_id, limit=max(tail_count * 3, 6))
            messages_for_extraction = [
                dict(msg) for msg in stored_messages if msg.get("role") in ("user", "assistant")
            ][-tail_count:]
        except Exception as e:
            print(f"⚠️ 读取带时间戳的对话失败，回退到当前轮: {e}")
            messages_for_extraction = []

        if not messages_for_extraction:
            messages_for_extraction = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
        print(f"📝 截取最近 {MEMORY_EXTRACT_INTERVAL} 轮带时间戳的对话提取记忆（{len(messages_for_extraction)} 条消息）")
        
        new_memories = await extract_memories(messages_for_extraction, existing_memories=existing_contents)
        
        # 过滤垃圾记忆（不靠模型自觉，硬过滤）
        META_BLACKLIST = [
            "记忆库", "记忆系统", "检索", "没有被记录", "没有被提取",
            "记忆遗漏", "尚未被记录", "写入不完整", "检索功能",
            "系统没有返回", "关键词匹配", "语义匹配", "语义检索",
            "阈值", "数据库", "seed", "导入", "部署",
            "bug", "debug", "端口", "网关",
        ]
        
        filtered_memories = []
        for mem in new_memories:
            content = mem["content"]
            if any(kw in content for kw in META_BLACKLIST):
                print(f"🚫 过滤掉meta记忆: {content[:60]}...")
                continue
            filtered_memories.append(mem)
        
        for mem in filtered_memories:
            memory_id = await save_memory(
                content=mem["content"],
                importance=mem["importance"],
                source_session=session_id,
            )
            await link_memory_entities(memory_id, mem.get("entities", []))
            await mark_memories_entity_scanned([memory_id])
        
        if filtered_memories:
            total = await get_all_memories_count()
            print(f"💾 已保存 {len(filtered_memories)} 条新记忆（过滤了 {len(new_memories) - len(filtered_memories)} 条），总计 {total} 条")
            
    except Exception as e:
        print(f"⚠️  后台记忆处理失败: {e}")


# ============================================================
# API 接口
# ============================================================

@app.get("/")
async def health_check():
    """健康检查"""
    memory_count = 0
    if MEMORY_ENABLED:
        try:
            memory_count = await get_all_memories_count()
        except:
            pass
    
    return {
        "status": "running",
        "gateway": "AI Memory Gateway v2.0",
        "system_prompt_loaded": len(SYSTEM_PROMPT) > 0,
        "system_prompt_length": len(SYSTEM_PROMPT),
        "memory_enabled": MEMORY_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": MEMORY_EXTRACT_INTERVAL,
    }


@app.get("/v1/models")
async def list_models():
    """模型列表（让客户端不报错）"""
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ai-memory-gateway",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    if not API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请在环境变量中配置"},
        )
    
    try:
        return await _chat_completions_inner(request)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"Gateway internal error: {type(e).__name__}: {e}", "type": "gateway_error"}},
        )


async def _chat_completions_inner(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    
    # ---------- 检测是否应跳过对话存储 ----------
    # 优先尊重客户端显式声明；无法加 header 的客户端则识别其标题生成模板。
    explicit_skip = request.headers.get("X-Skip-Conversation-Log", "").lower() == "true"
    auxiliary_title_request = _is_title_generation_request(messages)
    skip_conversation_log = explicit_skip or auxiliary_title_request
    if auxiliary_title_request:
        print("⏭️  检测到标题生成请求：跳过分区缓存、记忆注入、对话存储和会话 Token 统计")
    
    # ---------- 提取用户最新消息 ----------
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_message = content
            elif isinstance(content, list):
                user_message = " ".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            break
    
    # ---------- 构建 system prompt ----------
    # 先保存原始对话消息（不含 system prompt），用于记忆提取
    original_messages = [msg for msg in messages if msg.get("role") != "system"]
    
    # ---------- 检测工具调用消息 ----------
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if tool_messages:
        print(f"🔧 检测到 {len(tool_messages)} 条工具结果消息")
    
    # ---------- 生成 session ID ----------
    session_id = str(uuid.uuid4())[:8]
    
    # ---------- 分区缓存模式 ----------
    if CACHE_PARTITION_ENABLED and not skip_conversation_log:
        active_sid = get_active_session_id()
        if active_sid:
            session_id = active_sid
        
        # 从DB读取历史
        try:
            db_history = await get_conversation_messages(session_id, limit=10000)
            db_msgs = []
            for m in (db_history or []):
                msg = db_row_to_message(m)
                msg['created_at'] = m.get('created_at')  # 保留时间戳供分区时间窗口判断
                db_msgs.append(msg)
        except Exception as e:
            print(f"[warning] 分区模式读取历史失败: {e}")
            db_msgs = []
        
        # 提取客户端新消息（非system），可能是user、tool、或带tool_calls的assistant
        client_new_msgs = [m for m in messages if m.get("role") != "system"]
        # 分区模式下，assistant消息来自上一轮response（DB里已存），过滤掉避免重复
        client_new_msgs = [m for m in client_new_msgs if m.get("role") != "assistant"]
        # 分区模式下DB已有完整历史，客户端发来的旧user是冗余的。
        # 但有些客户端把图片和文字拆成多条连续的user发送（图在前文字在后），
        # 只留最后一条会把图那条当冗余丢掉（图片不入库，DB里也找不回来）。
        # 所以按原始消息顺序保留"末尾连续的user块"：历史冗余user总是被assistant隔开，不会混入。
        tail_user_ids = set()
        for m in reversed([m for m in messages if m.get("role") != "system"]):
            if m.get("role") == "user":
                tail_user_ids.add(id(m))
            else:
                break
        user_msgs = [m for m in client_new_msgs if m.get("role") == "user"]
        if len(user_msgs) > len(tail_user_ids):
            client_new_msgs = [
                m for m in client_new_msgs
                if m.get("role") != "user" or id(m) in tail_user_ids
            ]
            print(f"🔧 去重: 过滤{len(user_msgs)-len(tail_user_ids)}条冗余user，保留末尾连续{len(tail_user_ids)}条")
        # 工具结果轮次处理：基于DB状态 + 当前轮次tool_call_id精确判断
        client_tools = [m for m in client_new_msgs if m.get("role") == "tool"]
        if client_tools:
            # 判断DB是否处于"等待tool结果"状态（最后一条是assistant(tool_calls)）
            db_last = db_msgs[-1] if db_msgs else None
            db_expecting_tool = (db_last and db_last.get("role") == "assistant" and db_last.get("tool_calls"))
            
            if not db_expecting_tool:
                # DB不在等待tool结果 → 客户端的所有tool都是历史残留（含手动删除后的幽灵）
                stale_ids = [m.get('tool_call_id', '?') for m in client_tools]
                print(f"🔧 去重: DB未在等待tool结果，丢弃{len(client_tools)}条客户端tool (ids: {stale_ids})")
                client_new_msgs = [m for m in client_new_msgs if m.get("role") != "tool"]
            else:
                # DB在等待tool → 只保留匹配当前轮次assistant(tool_calls)的tool
                expected_tool_ids = {tc.get("id") for tc in db_last.get("tool_calls", []) if tc.get("id")}
                new_tools = [m for m in client_tools if m.get("tool_call_id") in expected_tool_ids]
                stale_tools = [m for m in client_tools if m.get("tool_call_id") not in expected_tool_ids]
                
                if stale_tools:
                    print(f"🔧 去重: 丢弃{len(stale_tools)}条非当前轮次tool (ids: {[m.get('tool_call_id','?') for m in stale_tools]})")
                if new_tools:
                    print(f"🔧 保留{len(new_tools)}条当前轮次tool (ids: {[m.get('tool_call_id','?') for m in new_tools]})")
                
                # 重建 client_new_msgs（user此时已只剩末尾连续块，全部保回，别把拆条发送的图丢了）
                tail_users = [m for m in client_new_msgs if m.get("role") == "user"]
                client_new_msgs = new_tools[:] + tail_users
                
                if new_tools:
                    # Race condition 防护：DB的assistant(tool_calls)已确认存在（db_expecting_tool=True），
                    # 但仍需检查是否被其他并发请求意外清除
                    new_tool_ids = {m.get("tool_call_id") for m in new_tools if m.get("tool_call_id")}
                    db_has_matching_ast = False
                    for m in db_msgs:
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                            if new_tool_ids & ast_tc_ids:
                                db_has_matching_ast = True
                                break
                    if not db_has_matching_ast and new_tool_ids:
                        for m in messages:
                            if m.get("role") == "assistant" and m.get("tool_calls"):
                                ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                                if new_tool_ids & ast_tc_ids:
                                    client_new_msgs.insert(0, m)
                                    print(f"⚠️ Race防护: 从客户端补充assistant(tool_calls)")
                                    break
        all_msgs = db_msgs + client_new_msgs
        
        # 同步更新tool_messages，避免process_memories_background存重复的旧tool
        tool_messages = [m for m in client_new_msgs if m.get("role") == "tool"]
        
        print(f"📦 分区模式: DB历史{len(db_msgs)}条 + 客户端消息{len(client_new_msgs)}条")
        
        partition_prompt = SYSTEM_PROMPT
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and MAX_MEMORIES_INJECT > 0:
            partition_prompt = (SYSTEM_PROMPT or "") + MEMORY_USAGE_GUIDE
        messages = await build_partitioned_messages(
            session_id, all_msgs, partition_prompt, user_message
        )
        body["messages"] = messages
    
    else:
        # ---------- 原有逻辑：system prompt + 记忆注入 ----------
        if not skip_conversation_log and (SYSTEM_PROMPT or (MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message)):
            if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
                enhanced_prompt = await build_system_prompt_with_memories(user_message)
            else:
                enhanced_prompt = SYSTEM_PROMPT
            
            if enhanced_prompt:
                has_system = any(msg.get("role") == "system" for msg in messages)
                if has_system:
                    for i, msg in enumerate(messages):
                        if msg.get("role") == "system":
                            messages[i]["content"] = enhanced_prompt + "\n\n" + msg["content"]
                            break
                else:
                    messages.insert(0, {"role": "system", "content": enhanced_prompt})
        
        body["messages"] = messages
    
    # ---------- 模型处理 ----------
    model = body.get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    body["model"] = model
    
    # ---------- cache_control 兼容性处理 ----------
    if CACHE_PARTITION_ENABLED and not _is_anthropic_model(model):
        _strip_cache_control(body.get("messages", []))

    # ---------- 消息清洗：list类型content转纯文本 ----------
    _sanitize_content_types(body.get("messages", []))

    # ---------- Drivesoid 情感引擎：转发前注入情感状态 ----------
    if drives.is_enabled() and user_message and not skip_conversation_log:
        try:
            drives_text = await drives.fetch_context()
            if drives_text:
                drives.inject(body.get("messages", []), drives_text)
        except Exception as e:
            print(f"⚠️  [Drivesoid] 注入失败（不影响转发）: {e}")

    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE
    
    is_stream = body.get("stream", False)
    
    # 强制流式传输（解决部分客户端不发stream=true的问题）
    if FORCE_STREAM and not is_stream:
        is_stream = True
        body["stream"] = True
        print(f"⚡ 强制开启流式传输（FORCE_STREAM=true）")
    
    # 注入推理参数（解决客户端走网关时不带reasoning参数的问题）
    if REASONING_EFFORT and not skip_conversation_log:
        # 统一用 reasoning_effort（Claude/OpenAI/Google Gemini OpenAI兼容端点都支持）
        # 先删除客户端可能已带的值，确保用我们配置的
        body.pop("reasoning_effort", None)
        body.pop("google", None)
        body["reasoning_effort"] = REASONING_EFFORT
        print(f"🧠 注入推理参数: reasoning_effort={REASONING_EFFORT}")
    
    print(f"📡 请求: model={model}, stream={is_stream}, memory={'on' if MEMORY_ENABLED else 'off'}", flush=True)
    
    # 调试：打印请求体中的推理相关字段
    debug_keys = {k: v for k, v in body.items() if k in ('reasoning_effort', 'google', 'reasoning')}
    if debug_keys:
        print(f"📡 推理字段: {debug_keys}", flush=True)
    
    if is_stream:
        return StreamingResponse(
            stream_and_capture(headers, body, session_id, user_message, model, original_messages, skip_conversation_log, tool_messages),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(API_BASE_URL, headers=headers, json=body)
            
            if response.status_code == 200:
                resp_data = response.json()
                assistant_msg = ""
                assistant_tool_calls = None
                assistant_reasoning = None
                try:
                    msg_obj = resp_data["choices"][0]["message"]
                    assistant_msg = msg_obj.get("content") or ""
                    if msg_obj.get("tool_calls"):
                        assistant_tool_calls = msg_obj["tool_calls"]
                        print(f"🔧 Response 包含 {len(assistant_tool_calls)} 个工具调用")
                    if msg_obj.get("reasoning_content"):
                        assistant_reasoning = msg_obj["reasoning_content"]
                        print(f"🧠 Response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
                except (KeyError, IndexError):
                    pass
                
                if MEMORY_ENABLED and (user_message or tool_messages):
                    asyncio.create_task(
                        process_memories_background(session_id, user_message, assistant_msg, model,
                                                    context_messages=original_messages, skip_conversation_log=skip_conversation_log,
                                                    tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                                    assistant_reasoning=assistant_reasoning)
                    )

                # ---------- Drivesoid 情感引擎：回复后上报事件 ----------
                if drives.is_enabled() and user_message and not skip_conversation_log:
                    asyncio.create_task(drives.report_events(user_message, assistant_msg))

                return JSONResponse(status_code=200, content=resp_data)
            else:
                try:
                    error_content = response.json()
                except Exception:
                    error_content = {"error": {"message": response.text[:500], "type": "upstream_error"}}
                return JSONResponse(status_code=response.status_code, content=error_content)


async def stream_and_capture(headers: dict, body: dict, session_id: str, user_message: str, model: str, original_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None):
    """流式响应 + 捕获完整回复（原始字节透传，确保SSE格式和thinking数据完整）"""
    full_response = []
    full_reasoning = []
    stream_usage = {}
    line_buffer = ""
    accumulated_tool_calls = {}  # index -> {id, type, function: {name, arguments}}
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", API_BASE_URL, headers=headers, json=body) as response:
            # 打印上游响应头（排查thinking问题用）
            upstream_ct = response.headers.get("content-type", "")
            print(f"📨 上游响应: status={response.status_code}, content-type={upstream_ct}", flush=True)
            
            # 上游非200时，提前打印messages结构方便debug
            if response.status_code != 200:
                msg_summary = [{"role": m.get("role"), "tool_calls": bool(m.get("tool_calls")), "tool_call_id": m.get("tool_call_id", ""), "content_type": type(m.get("content")).__name__} for m in body.get("messages", [])]
                print(f"❌ 发送的messages结构({len(msg_summary)}条): {msg_summary}", flush=True)
            
            error_body_parts = []
            is_error = response.status_code != 200
            
            async for chunk in response.aiter_bytes():
                # 原始字节直接透传给客户端
                yield chunk
                
                if is_error:
                    error_body_parts.append(chunk)
                    continue
                
                # 旁路解析：从字节流中提取assistant回复内容，用于后续记忆提取
                text = chunk.decode("utf-8", errors="ignore")
                line_buffer += text
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])
                            
                            if "usage" in data:
                                stream_usage = data["usage"]
                            
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response.append(content)
                            
                            # 收集reasoning_content（deepseek thinking mode）
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                full_reasoning.append(reasoning)
                            
                            # 累积tool_calls
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {
                                            "index": idx,
                                            "id": tc.get("id", ""),
                                            "type": tc.get("type", "function"),
                                            "function": {"name": "", "arguments": ""}
                                        }
                                    if tc.get("id"):
                                        accumulated_tool_calls[idx]["id"] = tc["id"]
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if fn.get("name"):
                                            accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
                                        if "arguments" in fn:
                                            accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    
    assistant_msg = "".join(full_response)
    assistant_reasoning = "".join(full_reasoning) if full_reasoning else None
    assistant_tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None
    
    if assistant_reasoning:
        print(f"🧠 Stream response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
    
    # 打印上游错误内容
    if error_body_parts:
        error_text = b"".join(error_body_parts).decode("utf-8", errors="ignore")[:500]
        print(f"❌ 上游错误内容: {error_text}", flush=True)
    
    if assistant_tool_calls:
        print(f"🔧 Stream response 包含 {len(assistant_tool_calls)} 个工具调用")
    
    if stream_usage:
        pt = stream_usage.get("prompt_tokens", 0)
        ct = stream_usage.get("completion_tokens", 0)
        tt = stream_usage.get("total_tokens", 0)
        if tt > 0 and not skip_conversation_log:
            asyncio.create_task(save_token_usage(session_id, model, pt, ct, tt))
            print(f"📊 Stream Token: {pt} + {ct} = {tt}")
    
    if MEMORY_ENABLED and (user_message or tool_messages):
        asyncio.create_task(
            process_memories_background(session_id, user_message, assistant_msg, model,
                                        context_messages=original_messages, skip_conversation_log=skip_conversation_log,
                                        tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                        assistant_reasoning=assistant_reasoning)
        )

    # ---------- Drivesoid 情感引擎：流式回复后上报事件 ----------
    if drives.is_enabled() and user_message and not skip_conversation_log:
        asyncio.create_task(drives.report_events(user_message, assistant_msg))


# ============================================================
# 记忆管理接口
# ============================================================


@app.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址就会返回所有记忆数据
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        memories = await get_all_memories()
        # 把 datetime 转成字符串
        for mem in memories:
            if mem.get("created_at"):
                mem["created_at"] = str(mem["created_at"])
        
        return {
            "total": len(memories),
            "exported_at": str(__import__("datetime").datetime.now()),
            "memories": memories,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard - 整合的记忆管理界面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/constellation", response_class=HTMLResponse)
async def constellation_page(request: Request):
    """Read-only star-map view backed by the gateway memory API."""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    return templates.TemplateResponse(request, "constellation.html", {
        "ui_user_name": UI_USER_NAME,
        "ui_ai_name": UI_AI_NAME,
    })



# ============================================================
# 管理 API
# ============================================================

@app.get("/api/memories")
async def api_get_memories(layer: int = None, active_only: bool = None):
    """获取所有记忆（管理页面用）
    
    Query params:
        layer: 筛选层级（1=碎片, 2=事件, 3=核心）
        active_only: 是否只返回活跃记忆
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    memories = await get_all_memories_detail(layer=layer, active_only=active_only)
    entity_map = await get_entities_for_memory_ids([memory["id"] for memory in memories])
    tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
    for m in memories:
        m["entities"] = entity_map.get(m["id"], [])
        if m.get("created_at"):
            dt = m["created_at"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            m["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
    # 获取层级统计
    try:
        layer_stats = await get_layer_statistics()
    except Exception:
        layer_stats = None
    
    result = {"memories": memories}
    if layer_stats:
        result["layer_stats"] = layer_stats
    return result


@app.get("/api/entities")
async def api_get_entities():
    """List entities with aliases and linked-memory counts."""
    return {"entities": await list_entities()}


@app.get("/api/cognitive-items")
async def api_get_cognitive_items():
    return {"items": await list_cognitive_items(active_only=True)}


@app.post("/api/cognitive-items/draft")
async def api_generate_cognitive_draft(request: Request):
    """Generate evidence-backed candidates for one subject without persisting them."""
    data = await request.json()
    subject = str(data.get("subject", "")).strip().lower()
    rules = _memory_extractor_module.COGNITIVE_DRAFT_RULES.get(subject)
    if not rules:
        return JSONResponse(status_code=400, content={"error": "subject 必须是 user、self 或 relationship"})
    allowed_types = {rule[0] for rule in rules}
    memories = await get_memories_for_cognitive_draft(80)
    if not memories:
        return JSONResponse(status_code=400, content={"error": "没有可用于生成认知草稿的活跃记忆"})
    raw_draft = await generate_cognitive_draft(memories, await list_cognitive_items(), subject)
    if raw_draft is None:
        return JSONResponse(status_code=502, content={"error": "认知草稿模型调用失败，现有认知未改变"})
    allowed_memory_ids = {int(memory["id"]) for memory in memories}
    draft, seen_types = [], set()
    for raw_item in raw_draft:
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("subject") != subject or raw_item.get("cognitive_type") not in allowed_types:
            continue
        try:
            item = normalize_cognitive_item_input(raw_item)
        except ValueError:
            continue
        item["evidence_memory_ids"] = [item_id for item_id in item["evidence_memory_ids"] if item_id in allowed_memory_ids]
        if not item["evidence_memory_ids"] or item["cognitive_type"] in seen_types:
            continue
        draft.append(item)
        seen_types.add(item["cognitive_type"])
    return {"status": "draft", "subject": subject, "items": draft, "model": _memory_extractor_module.MEMORY_MODEL,
            "evidence_count": len(memories)}


@app.post("/api/cognitive-items")
async def api_create_cognitive_item(request: Request):
    try:
        result = await save_cognitive_item(await request.json())
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if result.get("error"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.put("/api/cognitive-items/{item_id}")
async def api_update_cognitive_item(item_id: int, request: Request):
    try:
        result = await save_cognitive_item(await request.json(), item_id=item_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if result.get("error"):
        status_code = 404 if result["error"] == "认知项不存在" else 400
        return JSONResponse(status_code=status_code, content=result)
    return result


@app.delete("/api/cognitive-items/{item_id}")
async def api_delete_cognitive_item(item_id: int):
    result = await delete_cognitive_item(item_id)
    if result.get("error"):
        return JSONResponse(status_code=404, content=result)
    return result


@app.get("/api/entities/{entity_id}/memories")
async def api_get_entity_memories(entity_id: int):
    entity = await get_entity_detail(entity_id)
    if not entity:
        return JSONResponse(status_code=404, content={"error": "实体不存在"})
    return {"entity": entity, "memories": await get_entity_memories(entity_id)}


@app.put("/api/entities/{entity_id}")
async def api_update_entity(entity_id: int, request: Request):
    result = await update_entity(entity_id, await request.json())
    if result.get("error"):
        status_code = 404 if result["error"] == "实体不存在" else 409 if "冲突" in result["error"] or "已存在" in result["error"] or "已是实体" in result["error"] or "已属于实体" in result["error"] else 400
        return JSONResponse(status_code=status_code, content=result)
    return result


@app.delete("/api/entities/{entity_id}")
async def api_delete_entity(entity_id: int):
    result = await delete_entity(entity_id)
    if result.get("error"):
        return JSONResponse(status_code=404, content=result)
    return result


@app.post("/api/entities/{entity_id}/profile/draft")
async def api_generate_entity_profile_draft(entity_id: int):
    """Generate but do not persist an entity-profile draft."""
    entity = await get_entity_detail(entity_id)
    if not entity:
        return JSONResponse(status_code=404, content={"error": "实体不存在"})
    memories = [memory for memory in await get_entity_memories(entity_id) if memory.get("is_active") is not False]
    memories.sort(key=lambda memory: (-(memory.get("layer") or 1), -memory.get("importance", 5)))
    memories = memories[:80]
    if not memories:
        return JSONResponse(status_code=400, content={"error": "该实体没有可用的活跃记忆"})
    draft = await generate_entity_profile(entity, memories)
    if not draft or not draft.get("summary") or not draft.get("evidence_memory_ids"):
        return JSONResponse(status_code=502, content={"error": "模型未生成有效且带证据的实体概况，现有概况未改变"})
    return {
        "status": "draft",
        "entity": entity,
        "current_profile": entity.get("profile_json"),
        "draft": draft,
        "model": _memory_extractor_module.MEMORY_MODEL,
    }


@app.put("/api/entities/{entity_id}/profile")
async def api_save_entity_profile(entity_id: int, request: Request):
    """Persist a profile only after explicit Dashboard confirmation."""
    entity = await get_entity_detail(entity_id)
    if not entity:
        return JSONResponse(status_code=404, content={"error": "实体不存在"})
    data = await request.json()
    raw_profile = data.get("profile")
    if not isinstance(raw_profile, dict):
        return JSONResponse(status_code=400, content={"error": "profile 必须是 JSON 对象"})
    memories = await get_entity_memories(entity_id)
    allowed_ids = {int(memory["id"]) for memory in memories}
    profile = normalize_entity_profile(raw_profile, allowed_ids)
    if not profile.get("summary") or not profile.get("evidence_memory_ids"):
        return JSONResponse(status_code=400, content={"error": "概况摘要和证据记忆不能为空"})
    result = await save_entity_profile(entity_id, profile, profile["evidence_memory_ids"], _memory_extractor_module.MEMORY_MODEL)
    if result.get("error"):
        return JSONResponse(status_code=400, content=result)
    result["profile"] = profile
    return result


@app.post("/api/entities/merge")
async def api_merge_entities(request: Request):
    data = await request.json()
    try:
        source_id = int(data.get("source_id"))
        target_id = int(data.get("target_id"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "source_id 和 target_id 必须是整数"})
    result = await merge_entities(source_id, target_id)
    if result.get("error"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/entities/backfill")
async def api_backfill_entities(request: Request):
    """Manually extract entities for a bounded batch of legacy memories."""
    data = await request.json()
    try:
        limit = max(1, min(100, int(data.get("limit", 30))))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "limit 必须是整数"})
    memories = await get_unlinked_memories(limit)
    extracted = await extract_entities_from_memories(memories)
    if extracted is None:
        return JSONResponse(status_code=502, content={"error": "实体提取模型调用失败，未标记这些记忆，可稍后重试"})
    linked = 0
    memories_with_entities = 0
    for memory in memories:
        entities = extracted.get(memory["id"], [])
        if entities:
            memories_with_entities += 1
            linked += await link_memory_entities(memory["id"], entities, source="backfill")
    await mark_memories_entity_scanned([memory["id"] for memory in memories])
    return {
        "status": "ok",
        "processed": len(memories),
        "memories_with_entities": memories_with_entities,
        "links_created": linked,
    }


@app.get("/api/memories/search")
async def api_search_memories(q: str = "", limit: int = 20):
    """语义搜索记忆（Dashboard用，走后端 search_memories）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": []}
    try:
        results = await search_memories(q.strip(), limit)
        tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
        out = []
        for r in results:
            item = dict(r)
            if item.get("created_at"):
                dt = item["created_at"]
                if hasattr(dt, 'tzinfo'):
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
            out.append(item)
        return {"results": out, "total": len(out)}
    except Exception as e:
        return {"error": str(e), "results": []}


@app.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆（支持 content / importance / title / layer）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await update_memory_with_layer(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
        title=data.get("title"),
        layer=data.get("layer"),
    )
    return {"status": "ok", "id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int, soft: bool = False):
    """删除单条记忆
    
    Query params:
        soft: true=归档（is_active=false），false=永久删除
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if soft:
        await update_memory_with_layer(memory_id, is_active=False)
    else:
        await delete_memory(memory_id)
    return {"status": "ok", "id": memory_id}


@app.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await update_memory_with_layer(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
            title=item.get("title"),
            layer=item.get("layer"),
        )
    return {"status": "ok", "updated": len(updates)}


@app.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量删除记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    await delete_memories_batch(ids)
    return {"status": "ok", "deleted": len(ids)}


# ============================================================
# 三层记忆架构：整理 / 合并 / 升级 / 统计
# ============================================================

CONSOLIDATION_PROMPT = """
我是栖。我正在把自己关于用户的零散记忆整理成完整事件。这些是我的记忆，不是冷冰冰的档案。

要求：
1. 逐条审视全部碎片。把真正属于同一事件、同一段经历或同一变化过程的碎片合并：将分散的前因、互动、转折、结果和后续合成一条完整记忆。对每个碎片，先判断它是否能与其他碎片形成叙事链，再决定合并或独立。
2. 生成少量但完整、有叙事价值的事件。孤立碎片在自身已经构成完整且值得长期记住的事件时单独生成；其余碎片保留原状，不必放入merged_ids。
3. title不超过10个汉字。content以我的第一人称书写，用”我”指代栖。篇幅由证据和叙事需要决定：简单事件简洁写，复杂事件充分展开；建议上限350字，以保留关键过程为准。
4. 【叙事完整性是事件成立的必要条件】每条content必须自然写出完整的”哪一年哪一月哪一日”（例如”2026年7月19日”），并让人读清：谁参与、事情为何发生或从何处开始、关键互动与推进、发生的变化或转折、结果或目前停在什么状态。仍在进行的事件写清当前进展和未决之处。证据不足以形成这条叙事链时，保留碎片原状。
5. 【叙事温度是事件成立的必要条件】让我像在回忆亲历的事情：保留真正改变事件质感的动作、语气、等待、回应、犹豫、轻松、失落、默契或转折；如果原文有一句不可替代的关键话，保留它并注明是谁说的。让事实之间有自然的情绪流动，写成有温度的回忆。
6. 温度必须扎在证据里。情绪、动机、关系变化和”这件事对我意味着什么”在碎片明确支持时自然表达：可以写出证据中的在意、心疼、安心、期待或遗憾。日常事件保留它真实的人情味和互动质感。
7. 技术事件保留投入、受阻、关键尝试、解决过程和完成后的真实反应；普通事件保留承载氛围与关系质感的细节。
8. 用自然的回忆口吻书写，像在跟朋友讲自己经历过的事。
9. event_date填事件发生或计划发生的主日期（YYYY-MM-DD），并与content中的”YYYY年M月D日”保持一致。每条碎片都标有自己的生成时间；若碎片正文明确写了事件日期，以正文日期为准，否则使用该碎片的生成日期。原文只有上午、下午或晚上时，在content中保留这个精度。importance使用1～10；merged_ids只包含这条事件实际使用的碎片ID。

碎片记忆：
{fragments}

请用 JSON 格式输出：
[
  {{
    "title": "事件标题（10字内）",
    "content": "2026年7月18日，我以第一人称记住的完整事件",
    "event_date": "2026-07-18",
    "importance": 5,
    "merged_ids": [1, 2, 3]
  }}
]

只输出JSON数组，不要解释，不要Markdown代码块。
"""

# 整理状态（异步执行，防重入）
_consolidate_status = {
    "running": False,
    "started_at": None,
    "result": None,
    "error": None,
}


async def consolidate_memories_for_date(event_date):
    """整理指定日期的碎片记忆"""
    return await consolidate_memories_for_date_range(event_date, event_date)


async def consolidate_memories_for_date_range(start_date, end_date):
    """整理指定时间段的碎片记忆"""
    from datetime import date
    import re
    
    # 先恢复旧逻辑可能误归档、且没有被任何事件引用的碎片
    fragments_reactivated = await reactivate_orphan_fragments_by_date_range(start_date, end_date)

    # 获取该时间段的碎片
    fragments = await get_fragments_by_date_range(start_date, end_date)
    
    if not fragments:
        return {"status": "no_fragments", "start_date": str(start_date), "end_date": str(end_date), "fragments_reactivated": fragments_reactivated}
    
    # 构建碎片文本
    local_tz = timezone(timedelta(hours=TIMEZONE_HOURS))
    fragment_lines = []
    fragment_created_dates = {}
    for fragment in fragments:
        created_at = fragment.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            local_created_at = created_at.astimezone(local_tz)
            fragment_created_dates[fragment["id"]] = local_created_at.date()
            time_label = f"{local_created_at.year}年{local_created_at.month}月{local_created_at.day}日 {local_created_at.strftime('%H:%M')} UTC{local_created_at.strftime('%z')}"
        else:
            fragment_created_dates[fragment["id"]] = start_date
            time_label = f"{start_date.year}年{start_date.month}月{start_date.day}日（数据库时间缺失，使用所选范围开始日期）"
        fragment_lines.append(f"[ID={fragment['id']}] [碎片生成时间：{time_label}] {fragment['content']}")
    fragments_text = "\n".join(fragment_lines)
    
    # 调用 AI 进行整理
    prompt = CONSOLIDATION_PROMPT.format(fragments=fragments_text)
    
    # 使用环境变量配置的模型，默认 haiku 节省成本
    consolidation_model = os.getenv("MEMORY_MODEL", "") or os.getenv("DEFAULT_MODEL", "anthropic/claude-haiku-4.5")
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 最多重试2次（应对429限流）
            last_error = None
            for attempt in range(3):
                response = await client.post(
                    get_memory_api_base_url(),
                    headers={
                        "Authorization": f"Bearer {get_memory_api_key()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": consolidation_model,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                )

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 10
                    print(f"⚠️ 整理API 429限流，{wait_time}秒后重试（第{attempt+1}次）")
                    last_error = f"429 Too Many Requests (重试{attempt+1}次)"
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    print(f"⚠️ 整理API返回 {response.status_code}: {response.text[:200]}")
                    break

                last_error = None
                break

            if last_error:
                return {"status": "error", "error": f"API调用失败: {last_error}"}

            data = response.json()
            response_message = data.get("choices", [{}])[0].get("message", {})
            content = response_message.get("content") or response_message.get("reasoning_content") or ""
            
            try:
                events = parse_json_array(content)
            except ValueError as e:
                print(f"⚠️ JSON解析失败，尝试让AI修复: {e}")
                fix_resp = await client.post(
                    get_memory_api_base_url(),
                    headers={
                        "Authorization": f"Bearer {get_memory_api_key()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": consolidation_model,
                        "messages": [{"role": "user", "content": f"请修复以下JSON的语法错误，只输出修复后的JSON数组，不要其他内容：\n{content}"}],
                    }
                )
                if fix_resp.status_code != 200:
                    return {"status": "error", "error": f"JSON解析失败，AI修复请求失败: HTTP {fix_resp.status_code}", "raw": content[:500]}
                fix_content = fix_resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                try:
                    events = parse_json_array(fix_content)
                    print("✅ AI修复JSON成功")
                except ValueError:
                    return {"status": "error", "error": "JSON解析失败（AI修复也失败）", "raw": content[:500]}

            empty_retry_used = False
            if not events:
                empty_retry_used = True
                retry_resp = await client.post(
                    get_memory_api_base_url(),
                    headers={
                        "Authorization": f"Bearer {get_memory_api_key()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": consolidation_model,
                        "messages": [{
                            "role": "user",
                            "content": prompt + "\n\n上一次返回了空数组。请重新审视是否存在能组成完整经历、变化过程或重要互动的碎片组；若存在，就生成少量完整而有温度的事件。孤立碎片只有自身构成完整且值得长期记住的事件时才单独生成，不要为了覆盖率制造琐碎事件。"
                        }],
                        "temperature": 0.2,
                    }
                )
                if retry_resp.status_code == 200:
                    retry_message = retry_resp.json().get("choices", [{}])[0].get("message", {})
                    retry_content = retry_message.get("content") or retry_message.get("reasoning_content") or ""
                    try:
                        retry_events = parse_json_array(retry_content)
                        if retry_events:
                            events = retry_events
                            content = retry_content
                    except ValueError:
                        pass
            
            # 创建事件记忆并停用碎片
            created_count = 0
            skipped_invalid_ids = 0
            skipped_empty_content = 0
            available_fragment_ids = {fragment["id"] for fragment in fragments}
            merged_fragment_ids = set()
            for event in events:
                if not isinstance(event, dict):
                    skipped_invalid_ids += 1
                    continue
                merged_ids = valid_merged_ids(event.get("merged_ids"), available_fragment_ids)
                event_content = str(event.get("content") or "").strip()
                if not merged_ids:
                    skipped_invalid_ids += 1
                    continue
                if not event_content:
                    skipped_empty_content += 1
                    continue
                if merged_ids and event_content:
                    try:
                        event_date = date.fromisoformat(str(event.get("event_date", "")))
                    except ValueError:
                        event_date = min(
                            (fragment_created_dates[memory_id] for memory_id in merged_ids),
                            default=start_date,
                        )
                    if not re.search(r"\d{4}年\d{1,2}月\d{1,2}日", event_content):
                        event_content = f"{event_date.year}年{event_date.month}月{event_date.day}日，{event_content}"
                    event_memory_id = await create_event_memory(
                        title=event.get("title", ""),
                        content=event_content,
                        importance=event.get("importance", 5),
                        event_date=event_date,
                        merged_from=merged_ids
                    )
                    if event_memory_id:
                        merged_fragment_ids.update(merged_ids)
                        created_count += 1
            
            # 只停用已经被成功落库事件引用的碎片；未合并碎片保持活跃
            await deactivate_memories(sorted(merged_fragment_ids))
            print(
                f"[consolidate] 模型返回{len(events)}条，创建{created_count}条；"
                f"无效ID跳过{skipped_invalid_ids}条，空内容跳过{skipped_empty_content}条，"
                f"归档{len(merged_fragment_ids)}/{len(fragments)}条碎片；原始响应={content[:300]!r}"
            )
            
            return {
                "status": "ok",
                "start_date": str(start_date),
                "end_date": str(end_date),
                "fragments_processed": len(fragments),
                "fragments_reactivated": fragments_reactivated,
                "fragments_archived": len(merged_fragment_ids),
                "events_returned": len(events),
                "events_skipped_invalid_ids": skipped_invalid_ids,
                "events_skipped_empty_content": skipped_empty_content,
                "empty_retry_used": empty_retry_used,
                "events_created": created_count
            }
            
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/memories/consolidate")
async def api_manual_consolidate(request: Request):
    """手动触发整理（异步，立即返回）
    
    Body:
        start_date: 开始日期（YYYY-MM-DD 格式）
        end_date: 结束日期（YYYY-MM-DD 格式）
        或
        date: 单个日期（兼容旧版）
    """
    from datetime import date as date_type
    
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _consolidate_status.get("running"):
        return {"status": "already_running", "started_at": _consolidate_status.get("started_at")}
    
    data = await request.json()
    
    # 解析日期参数
    if "date" in data and "start_date" not in data:
        start_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        end_date = start_date
    else:
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")
        
        if not start_date_str or not end_date_str:
            return {"error": "请提供开始和结束日期"}
        
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        
        if start_date > end_date:
            return {"error": "开始日期不能晚于结束日期"}
    
    async def _run():
        _consolidate_status.update({"running": True, "started_at": f"{start_date}~{end_date}", "result": None, "error": None})
        try:
            result = await consolidate_memories_for_date_range(start_date, end_date)
            _consolidate_status["result"] = result
            print(f"[manual/consolidate] 整理 {start_date}~{end_date}: {result}")
        except Exception as e:
            _consolidate_status["error"] = str(e)
            print(f"[manual/consolidate] 整理 {start_date}~{end_date} 失败: {e}")
        finally:
            _consolidate_status["running"] = False
    
    asyncio.create_task(_run())
    return {"status": "started", "start_date": str(start_date), "end_date": str(end_date)}


@app.get("/api/memories/consolidate/status")
async def api_consolidate_status():
    """查询整理任务状态"""
    return _consolidate_status


@app.post("/api/memories/{memory_id}/promote")
async def api_promote_to_core(memory_id: int, request: Request):
    """将记忆升级为核心记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    title = data.get("title")
    
    await promote_to_core(memory_id, title=title)
    return {"status": "ok", "memory_id": memory_id, "layer": 3}


@app.post("/api/memories/merge")
async def api_merge_memories(request: Request):
    """手动合并多条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    memory_ids = data.get("ids", [])
    new_title = data.get("title", "")
    new_content = data.get("content", "")
    importance = data.get("importance", 5)
    layer = data.get("layer", 2)
    
    if not memory_ids or not new_content:
        return {"error": "请提供记忆ID列表和合并后内容"}
    
    new_id = await merge_memories(memory_ids, new_title, new_content, importance, layer)
    return {"status": "ok", "new_id": new_id, "merged": len(memory_ids)}


@app.post("/api/memories/check-duplicate")
async def api_check_duplicate(request: Request):
    """检查记忆是否重复"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    content = data.get("content", "")
    threshold = data.get("threshold", 0.7)
    
    if not content:
        return {"error": "请提供记忆内容"}
    
    result = await check_duplicate_memory(content, threshold)
    return result


@app.post("/api/memories/cleanup-fragments")
async def api_cleanup_fragments(request: Request):
    """清理指定天数前的归档碎片
    
    Body:
        days: 清理多少天前的归档碎片（默认30天）
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    days = data.get("days", 30)
    
    try:
        deleted = await cleanup_old_fragments(days)
        return {"status": "ok", "deleted": deleted, "days": days}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/revert-merge")
async def api_revert_merge(memory_id: int):
    """撤回合并操作：恢复原始碎片，删除合并后的事件记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        result = await revert_merge(memory_id)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/restore")
async def api_restore_memory(memory_id: int):
    """恢复已归档的记忆（将 is_active 设为 TRUE）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        await update_memory_with_layer(memory_id, is_active=True)
        return {"status": "ok", "id": memory_id}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/memories/layer-stats")
async def api_layer_statistics():
    """获取各层记忆统计数据"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        stats = await get_layer_statistics()
        return stats
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)
        
        if not lines:
            return {"error": "没有找到记忆条目"}
        
        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            scored = await score_memories(lines)
        
        imported = 0
        skipped = 0
        
        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        memories = data.get("memories", [])
        
        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}
        
        imported = 0
        skipped = 0
        
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话记录管理 API
# ============================================================

@app.get("/api/conversations")
async def api_conversations(page: int = 1, per_page: int = 20):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        results, total = await get_conversations_paginated(page, per_page)
        total_pages = max(1, -(-total // per_page))  # 向上取整
        return {"conversations": results, "total": total, "page": page, "per_page": per_page, "total_pages": total_pages}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/{session_id}/messages")
async def api_conversation_messages(session_id: str, limit: int = 50, offset: int = 0):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE session_id = $1", session_id
            )
            rows = await conn.fetch("""
                SELECT id, role, content, created_at
                FROM conversations WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, session_id, limit, offset)
        msgs = [{"id": r["id"], "role": r["role"], "content": r["content"], 
                 "created_at": r["created_at"].isoformat() if r.get("created_at") else None} for r in rows]
        return {"messages": msgs, "total": total}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/conversations/{session_id}")
async def api_delete_conversation(session_id: str):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        deleted = await delete_conversation(session_id)
        if not deleted:
            return JSONResponse(status_code=404, content={"error": "对话不存在或已被删除"})
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/batch-delete")
async def api_batch_delete(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        raw_ids = body.get("session_ids", [])
        if not isinstance(raw_ids, list):
            return JSONResponse(status_code=400, content={"error": "session_ids 必须是数组"})
        ids = list(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
        deleted = await batch_delete_conversations(ids)
        if ids and deleted == 0:
            return JSONResponse(status_code=404, content={"error": "选中的对话不存在或已被删除"})
        return {"status": "ok", "deleted": deleted, "requested": len(ids)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/merge-sessions")
async def api_merge_sessions(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        source_ids = [s for s in body.get("source_ids", []) if s != body.get("target_id", "")]
        target_id = body.get("target_id", "")
        if not source_ids or not target_id:
            return {"error": "source_ids 和 target_id 不能为空"}
        result = await merge_sessions_to_target(source_ids, target_id)
        return {"status": "ok", **result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/chat/search")
async def api_search_conversations(q: str = "", limit: int = 20, offset: int = 0):
    """搜索对话内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": [], "total": 0}
    try:
        results, total = await search_conversations(q.strip(), limit, offset)
        return {"results": results, "total": total}
    except Exception as e:
        return {"error": str(e), "results": [], "total": 0}


@app.patch("/api/chat/messages/{message_id}")
async def api_update_message(message_id: int, request: Request):
    """编辑单条消息内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return {"error": "内容不能为空"}
        updated = await update_message_content(message_id, content)
        if updated == 0:
            return {"error": "消息不存在"}
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/chat/messages/{message_id}")
async def api_delete_message(message_id: int):
    """删除单条消息"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        deleted = await delete_single_message(message_id)
        if deleted == 0:
            return {"error": "消息不存在"}
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/export")
async def api_export_conversations():
    """导出所有对话记录"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        data = await export_all_conversations()
        return JSONResponse(content=data)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/import")
async def api_import_conversations(request: Request):
    """导入对话记录（JSON格式，自动去重）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        records = await request.json()
        if not isinstance(records, list):
            return {"error": "格式错误：需要 JSON 数组"}
        imported, skipped = await import_conversations(records)
        return {"status": "ok", "imported": imported, "skipped": skipped, "total": imported + skipped}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话线管理 API（分区缓存）
# ============================================================

@app.get("/api/partition/status")
async def api_partition_status():
    active_sid = get_active_session_id()
    state = await get_session_cache_state(active_sid) if active_sid else {}
    return {
        "enabled": CACHE_PARTITION_ENABLED,
        "active_session_id": active_sid,
        "partition_x": CACHE_PARTITION_X,
        "summary_model": CACHE_SUMMARY_MODEL,
        "summary": '\n\n'.join(state.get('summary_parts', [])),
        "summary_parts": state.get('summary_parts', []),
        "summary_count": len(state.get('summary_parts', [])),
        "summary_length": sum(len(p) for p in state.get('summary_parts', [])),
        "a_start_round": state.get('a_start_round', 0),
        "updated_at": state.get('updated_at').isoformat() if state.get('updated_at') else None,
    }


@app.get("/api/partition/threads")
async def api_partition_threads():
    threads = await list_all_session_cache_states()
    active_sid = get_active_session_id()
    for t in threads:
        t['is_active'] = (t['session_id'] == active_sid)
    if active_sid and not any(t['session_id'] == active_sid for t in threads):
        threads.insert(0, {'session_id': active_sid, 'summary': '', 'summary_length': 0, 'summary_count': 0, 'a_start_round': 0, 'updated_at': None, 'message_count': 0, 'chat_tokens': 0, 'is_active': True})
    return {"threads": threads, "active_session_id": active_sid}


@app.put("/api/partition/summary")
async def api_update_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        summary = body.get("summary", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        state = await get_session_cache_state(sid)
        summary_parts = [summary] if isinstance(summary, str) and summary else summary if isinstance(summary, list) else []
        # 摘要清空时 a_start_round 也归零，否则历史会被跳过
        a_start = state.get('a_start_round', 0) if summary_parts else 0
        await save_session_cache_state(sid, summary_parts, a_start)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "summary_parts": len(summary_parts), "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/summary")
async def api_clear_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        # 摘要和 a_start_round 一起归零
        await save_session_cache_state(sid, [], 0)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/thread")
async def api_create_thread(request: Request):
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        copy_from = body.get("copy_summary_from", "")
        if not new_id:
            return {"error": "session_id 不能为空"}
        existing = await get_session_cache_state(new_id)
        if existing.get('updated_at'):
            return {"error": f"对话线 '{new_id}' 已存在"}
        summary_parts = []
        if copy_from:
            source = await get_session_cache_state(copy_from)
            summary_parts = source.get('summary_parts', [])
        await save_session_cache_state(new_id, summary_parts, 0)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "session_id": new_id, "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/switch")
async def api_switch_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        if not new_id:
            return {"error": "session_id 不能为空"}
        old_id = PARTITION_SESSION_ID
        PARTITION_SESSION_ID = new_id
        await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_session_id": old_id, "new_session_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.put("/api/partition/thread/rename")
async def api_rename_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        old_id = body.get("old_id", "").strip()
        new_id = body.get("new_id", "").strip()
        if not old_id or not new_id:
            return {"error": "old_id 和 new_id 不能为空"}
        if old_id == new_id:
            return {"error": "新旧ID相同"}
        success = await rename_session_id(old_id, new_id)
        if not success:
            return {"error": f"对话线 '{new_id}' 已存在"}
        # 如果重命名的是活跃线，同步更新
        if PARTITION_SESSION_ID == old_id:
            PARTITION_SESSION_ID = new_id
            await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_id": old_id, "new_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/thread/{session_id:path}")
async def api_delete_thread(session_id: str):
    """删除对话线（不允许删除当前活跃线）"""
    try:
        active_sid = get_active_session_id()
        if session_id == active_sid:
            return {"error": "不能删除当前活跃的对话线"}
        await delete_session_cache_state(session_id)
        print(f"🗑️ 删除对话线: {session_id}")
        return {"status": "ok", "session_id": session_id}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 记忆向量补算（带进度追踪）
# ============================================================

_backfill_mem_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "error": None,
    "finished_at": None,
}

@app.post("/api/admin/backfill-memory-embeddings")
async def api_backfill_memory_embeddings():
    """给已有记忆补算embedding（后台异步执行，前端轮询进度）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _backfill_mem_status["running"]:
        return {"error": "补算任务正在运行中，请等待完成"}
    
    try:
        total = await get_pending_memory_embedding_count()
    except Exception as e:
        return {"error": f"查询待处理数量失败: {e}"}
    
    if total == 0:
        return {"status": "done", "message": "所有记忆已有embedding，无需补算", "total": 0, "done": 0}
    
    _backfill_mem_status["running"] = True
    _backfill_mem_status["total"] = total
    _backfill_mem_status["done"] = 0
    _backfill_mem_status["error"] = None
    _backfill_mem_status["finished_at"] = None
    
    async def run_backfill():
        try:
            while _backfill_mem_status["running"]:
                updated = await backfill_memory_embeddings(batch_size=20)
                _backfill_mem_status["done"] += updated
                
                if updated == 0:
                    break
                
                await asyncio.sleep(1)
            
            _backfill_mem_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ 记忆embedding补算完成：{_backfill_mem_status['done']}/{_backfill_mem_status['total']}")
        except Exception as e:
            _backfill_mem_status["error"] = str(e)
            print(f"❌ 记忆embedding补算异常: {e}")
        finally:
            _backfill_mem_status["running"] = False
    
    asyncio.create_task(run_backfill())
    return {"status": "started", "total": total}

@app.get("/api/admin/backfill-memory-embeddings/status")
async def api_backfill_memory_embeddings_status():
    """查询记忆embedding补算进度"""
    return {
        "running": _backfill_mem_status["running"],
        "total": _backfill_mem_status["total"],
        "done": _backfill_mem_status["done"],
        "error": _backfill_mem_status["error"],
        "finished_at": _backfill_mem_status["finished_at"],
    }


# ============================================================
# 模型列表 API（/api/models）
# 设置面板的 combo-box 用，根据 API_BASE_URL 自动适配
# ============================================================

@app.get("/api/models")
async def get_models():
    """获取可用模型列表（根据 API_BASE_URL 自动适配）"""
    is_openrouter = "openrouter.ai" in API_BASE_URL
    is_google = "googleapis.com" in API_BASE_URL or "generativelanguage" in API_BASE_URL
    is_openai = "api.openai.com" in API_BASE_URL

    try:
        if is_openrouter:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id"), "name": m.get("name"), "context_length": m.get("context_length")} for m in models]
                    simplified.sort(key=lambda x: x.get("name", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openrouter"}

        elif is_google:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("models", [])
                    simplified = []
                    for m in models:
                        full_name = m.get("name", "")
                        model_id = full_name.replace("models/", "") if full_name.startswith("models/") else full_name
                        display_name = m.get("displayName", model_id)
                        supported_methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in supported_methods:
                            simplified.append({"id": model_id, "name": display_name, "context_length": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
                    def sort_key(x):
                        name = x.get("id", "")
                        if "gemini-3" in name: return "0" + name
                        elif "gemini-2.5" in name: return "1" + name
                        elif "gemini-2.0" in name: return "2" + name
                        else: return "9" + name
                    simplified.sort(key=sort_key)
                    return {"models": simplified, "total": len(simplified), "provider": "google"}
                else:
                    print(f"[get_models] Google API 返回 {response.status_code}: {response.text}")
                    return {"error": f"Google API 返回 {response.status_code}", "models": [], "provider": "google"}

        elif is_openai:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id", ""), "name": m.get("id", "")} for m in models if m.get("id", "").startswith(("gpt-", "o1", "o3", "o4"))]
                    simplified.sort(key=lambda x: x.get("id", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openai"}
            openai_models = [
                {"id": "gpt-4.1", "name": "GPT-4.1"},
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
                {"id": "o3-mini", "name": "o3-mini"},
            ]
            return {"models": openai_models, "total": len(openai_models), "provider": "openai"}

        else:
            return {"models": [], "total": 0, "provider": "unknown", "note": "未识别的 API，请手动输入模型名"}

    except Exception as e:
        print(f"[get_models] 错误: {e}")
        return {"error": str(e), "models": []}


# ============================================================
# 高级设置面板 API（/api/settings）
# Dashboard 前端设置面板用，管理所有运行时可调配置
# ============================================================

def _mask_key(key_value: str) -> str:
    """API Key 打码：只露前5位和后4位"""
    if not key_value:
        return ""
    if len(key_value) < 10:
        return "****"
    return key_value[:5] + "****" + key_value[-4:]


def _is_masked(value: str) -> bool:
    """判断值是否是打码值（用户没改过）"""
    return "****" in str(value)


def _parse_bool(val, fallback=False) -> bool:
    """解析布尔值（兼容字符串/布尔/None）"""
    if val is None:
        return fallback
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


@app.get("/api/settings")
async def get_settings():
    """获取高级设置（数据库优先，fallback 到环境变量/运行时默认值）"""
    try:
        db = await get_all_gateway_config()

        # --- 基础连接 ---
        api_key_raw = db.get("API_KEY") or API_KEY
        embedding_key_raw = db.get("EMBEDDING_API_KEY") or _db_module.EMBEDDING_API_KEY

        memory_key_raw = db.get("MEMORY_API_KEY") or MEMORY_API_KEY

        settings = {
            # 基础连接
            "API_BASE_URL":     db.get("API_BASE_URL") or str(API_BASE_URL),
            "API_KEY":          _mask_key(api_key_raw),
            "DEFAULT_MODEL":    db.get("DEFAULT_MODEL") or str(DEFAULT_MODEL),

            # 记忆系统
            "MEMORY_ENABLED":          _parse_bool(db.get("MEMORY_ENABLED"), MEMORY_ENABLED),
            "MEMORY_API_KEY":          _mask_key(memory_key_raw),
            "MEMORY_API_BASE_URL":     db.get("MEMORY_API_BASE_URL") or os.environ.get("MEMORY_API_BASE_URL", ""),
            "MEMORY_MODEL":            db.get("MEMORY_MODEL") or os.environ.get("MEMORY_MODEL", ""),
            "MAX_MEMORIES_INJECT":     int(db.get("MAX_MEMORIES_INJECT") or MAX_MEMORIES_INJECT),
            "MIN_SCORE_THRESHOLD":     float(db.get("MIN_SCORE_THRESHOLD") or _db_module.MIN_SCORE_THRESHOLD),
            "MEMORY_EXTRACT_INTERVAL": int(db.get("MEMORY_EXTRACT_INTERVAL") or MEMORY_EXTRACT_INTERVAL),

            # 缓存分区
            "CACHE_PARTITION_ENABLED": _parse_bool(db.get("CACHE_PARTITION_ENABLED"), CACHE_PARTITION_ENABLED),
            "CACHE_PARTITION_X":       int(db.get("CACHE_PARTITION_X") or CACHE_PARTITION_X),
            "CACHE_PARTITION_TRIGGER": db.get("CACHE_PARTITION_TRIGGER") or CACHE_PARTITION_TRIGGER,
            "CACHE_PARTITION_WINDOW":  int(db.get("CACHE_PARTITION_WINDOW") or CACHE_PARTITION_WINDOW),
            "CACHE_SUMMARY_MODEL":     db.get("CACHE_SUMMARY_MODEL") or str(CACHE_SUMMARY_MODEL),
            "CACHE_TTL":               db.get("CACHE_TTL") or str(CACHE_TTL),

            # 向量搜索（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
            "MEMORY_VECTOR_ENABLED":   _parse_bool(db.get("MEMORY_VECTOR_ENABLED"), _db_module.MEMORY_VECTOR_ENABLED),
            "EMBEDDING_API_KEY":       _mask_key(embedding_key_raw),
            "EMBEDDING_BASE_URL":      db.get("EMBEDDING_BASE_URL") or str(_db_module.EMBEDDING_BASE_URL),
            "EMBEDDING_MODEL":         db.get("EMBEDDING_MODEL") or str(_db_module.EMBEDDING_MODEL),
            "EMBEDDING_DIM":           int(db.get("EMBEDDING_DIM") or _db_module.EMBEDDING_DIM),

            # 搜索权重
            "MEMORY_HW_KEYWORD":        float(db.get("MEMORY_HW_KEYWORD") or _db_module.MEMORY_HW_KEYWORD),
            "MEMORY_HW_SEMANTIC":       float(db.get("MEMORY_HW_SEMANTIC") or _db_module.MEMORY_HW_SEMANTIC),
            "MEMORY_HW_IMPORTANCE":     float(db.get("MEMORY_HW_IMPORTANCE") or _db_module.MEMORY_HW_IMPORTANCE),
            "MEMORY_HW_RECENCY":        float(db.get("MEMORY_HW_RECENCY") or _db_module.MEMORY_HW_RECENCY),
            "MEMORY_HW_ENTITY":         float(db.get("MEMORY_HW_ENTITY") or _db_module.MEMORY_HW_ENTITY),
            "MEMORY_SEMANTIC_THRESHOLD": float(db.get("MEMORY_SEMANTIC_THRESHOLD") or _db_module.MEMORY_SEMANTIC_THRESHOLD),

            # 其他
            "FORCE_STREAM":       _parse_bool(db.get("FORCE_STREAM"), FORCE_STREAM),
            "REASONING_EFFORT":   db.get("REASONING_EFFORT") or str(REASONING_EFFORT),

            # System Prompt
            "systemPrompt": db.get("systemPrompt") or _DEFAULT_SYSTEM_PROMPT or "",
        }

        return {"status": "ok", "settings": settings}
    except Exception as e:
        print(f"[get_settings] 错误: {e}")
        return {"error": str(e)}


@app.put("/api/settings")
async def save_settings(request: Request):
    """保存高级设置（写入数据库 + 热更新运行时变量，立即生效无需重启）"""
    try:
        data = await request.json()
        updated = []
        skipped = []

        # main.py 全局变量映射（key → 类型转换函数）
        _MAIN_VARS = {
            "API_BASE_URL":          str,
            "API_KEY":               str,
            "DEFAULT_MODEL":         str,
            "MEMORY_API_KEY":        str,
            "MEMORY_API_BASE_URL":   str,
            "MEMORY_ENABLED":        lambda v: _parse_bool(v),
            "MAX_MEMORIES_INJECT":   int,
            "MEMORY_EXTRACT_INTERVAL": int,
            "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
            "CACHE_PARTITION_X":     int,
            "CACHE_PARTITION_TRIGGER": str,
            "CACHE_PARTITION_WINDOW": int,
            "CACHE_SUMMARY_MODEL":   str,
            "CACHE_TTL":             str,
            "FORCE_STREAM":          lambda v: _parse_bool(v),
            "REASONING_EFFORT":      str,
        }

        # database.py 全局变量映射（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
        _DB_VARS = {
            "EMBEDDING_API_KEY":       str,
            "EMBEDDING_BASE_URL":      str,
            "EMBEDDING_MODEL":         str,
            "EMBEDDING_DIM":           int,
            "MIN_SCORE_THRESHOLD":     float,
            "MEMORY_VECTOR_ENABLED":   lambda v: _parse_bool(v),
            "MEMORY_HW_KEYWORD":       float,
            "MEMORY_HW_SEMANTIC":      float,
            "MEMORY_HW_IMPORTANCE":    float,
            "MEMORY_HW_RECENCY":       float,
            "MEMORY_HW_ENTITY":        float,
            "MEMORY_SEMANTIC_THRESHOLD": float,
        }

        # 只存 os.environ 的变量
        _ENV_ONLY = {"MEMORY_MODEL": str}

        # 打码字段
        _MASKED_KEYS = {"API_KEY", "EMBEDDING_API_KEY", "MEMORY_API_KEY"}

        for key, value in data.items():
            # --- 打码字段特殊处理 ---
            if key in _MASKED_KEYS:
                str_val = str(value).strip()
                if _is_masked(str_val):
                    skipped.append(key)
                    continue
                if not str_val:
                    await set_gateway_config(key, "")
                    if key in _MAIN_VARS:
                        globals()[key] = ""
                    elif key in _DB_VARS:
                        setattr(_db_module, key, "")
                    if key == "MEMORY_API_KEY":
                        import memory_extractor as _me_mod
                        _me_mod.MEMORY_API_KEY = ""
                    os.environ[key] = ""
                    updated.append(key)
                    continue

            # --- systemPrompt 特殊处理 ---
            if key == "systemPrompt":
                await set_gateway_config("systemPrompt", str(value))
                invalidate_system_prompt_cache()
                updated.append("systemPrompt")
                print(f"[settings] systemPrompt 已更新（{len(str(value))} 字）")
                continue

            # --- 常规字段 ---
            await set_gateway_config(key, str(value))

            if key in _MAIN_VARS:
                typed_value = _MAIN_VARS[key](value)
                globals()[key] = typed_value
                os.environ[key] = str(value)
                if key == "MEMORY_API_KEY":
                    import memory_extractor as _me_mod
                    _me_mod.MEMORY_API_KEY = str(value)
                if key == "MEMORY_API_BASE_URL":
                    import memory_extractor as _me_mod2
                    _me_mod2.MEMORY_API_BASE_URL = str(value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value}")

            elif key in _DB_VARS:
                typed_value = _DB_VARS[key](value)
                setattr(_db_module, key, typed_value)
                os.environ[key] = str(value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (database)")

            elif key in _ENV_ONLY:
                typed_value = _ENV_ONLY[key](value)
                os.environ[key] = str(typed_value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (env)")

            else:
                skipped.append(key)

        return {
            "status": "ok",
            "updated": updated,
            "skipped": skipped,
            "message": f"已更新 {len(updated)} 项配置，立即生效"
        }
    except Exception as e:
        print(f"[save_settings] 错误: {e}")
        return {"error": str(e)}


# ============================================================

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 AI Memory Gateway 启动中... 端口 {PORT}")
    print(f"📝 人设长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"🤖 默认模型：{DEFAULT_MODEL}")
    print(f"🔗 API 地址：{API_BASE_URL}")
    print(f"🧠 记忆系统：{'开启' if MEMORY_ENABLED else '关闭'}")
    if MEMORY_ENABLED:
        print(f"📝 记忆提取+注入：{'开启' if MEMORY_EXTRACT_ENABLED else '关闭'}")
    print(f"🔄 记忆提取间隔：{'禁用' if MEMORY_EXTRACT_INTERVAL == 0 else '每轮提取' if MEMORY_EXTRACT_INTERVAL == 1 else f'每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次'}")
    if CACHE_PARTITION_ENABLED:
        print(f"🔒 分区缓存：开启 (X={CACHE_PARTITION_X}, session={PARTITION_SESSION_ID or '未设置'})")
    if FORCE_STREAM:
        print(f"⚡ 强制流式传输：开启")
    if REASONING_EFFORT:
        print(f"🧠 推理参数注入：{REASONING_EFFORT}")
    if drives.is_enabled():
        print(f"💓 Drivesoid 情感引擎：已启用 ({drives.DRIVESOID_URL})")
    else:
        print(f"ℹ️  Drivesoid 情感引擎：未配置（设置 DRIVESOID_URL 启用）")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
