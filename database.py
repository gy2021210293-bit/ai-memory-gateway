"""
数据库模块 —— 负责所有跟 PostgreSQL 打交道的事情
==============================================
包括：
- 创建表结构
- 存储对话记录
- 存储/检索记忆（带中文分词和加权排序）
"""

import os
import re
from typing import Optional, List
from datetime import datetime, timedelta, timezone as dt_timezone

import asyncpg

# 时区偏移（和 main.py 保持一致）
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

DATABASE_URL = os.getenv("DATABASE_URL", "")

HAS_PGVECTOR = False  # 在init_tables时检测

# Embedding 配置（向量搜索用）
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "256"))

# 记忆向量搜索开关（需要同时设置 EMBEDDING_API_KEY）
MEMORY_VECTOR_ENABLED = os.getenv("MEMORY_VECTOR_ENABLED", "false").lower() == "true"

# 记忆搜索权重（纯关键词模式）
WEIGHT_KEYWORD = float(os.getenv("WEIGHT_KEYWORD", "0.5"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.3"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.2"))
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.15"))

# 记忆混合搜索权重（MEMORY_VECTOR_ENABLED=true 时生效）
MEMORY_HW_KEYWORD = float(os.getenv("MEMORY_HW_KEYWORD", "0.35"))
MEMORY_HW_SEMANTIC = float(os.getenv("MEMORY_HW_SEMANTIC", "0.35"))
MEMORY_HW_IMPORTANCE = float(os.getenv("MEMORY_HW_IMPORTANCE", "0.15"))
MEMORY_HW_RECENCY = float(os.getenv("MEMORY_HW_RECENCY", "0.15"))
MEMORY_SEMANTIC_THRESHOLD = float(os.getenv("MEMORY_SEMANTIC_THRESHOLD", "0.5"))
MEMORY_HW_ENTITY = float(os.getenv("MEMORY_HW_ENTITY", "0.25"))


# ============================================================
# 连接池管理
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未设置！")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
        print("✅ 数据库连接池已创建")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("✅ 数据库连接池已关闭")


# ============================================================
# 表结构初始化
# ============================================================

async def init_tables():
    global HAS_PGVECTOR
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT,
                model           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                metadata        TEXT
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id              SERIAL PRIMARY KEY,
                content         TEXT NOT NULL,
                importance      INTEGER DEFAULT 5,
                source_session  TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_accessed   TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_fts 
            ON memories 
            USING gin(to_tsvector('simple', content));
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_session 
            ON conversations (session_id, created_at);
        """)
        
        # 工具调用支持：加 metadata 字段（已有表自动迁移）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'conversations' AND column_name = 'metadata'
                ) THEN
                    ALTER TABLE conversations ADD COLUMN metadata TEXT;
                END IF;
            END $$;
        """)
        
        # content 允许 NULL（工具调用时 assistant 的 content 可能为空）
        await conn.execute("""
            ALTER TABLE conversations ALTER COLUMN content DROP NOT NULL;
        """)
        
        # 网关配置表（存储运行时可变配置）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_config (
                key     TEXT PRIMARY KEY,
                value   TEXT DEFAULT ''
            );
        """)
        
        # 分区缓存状态表（存储每个session的轮转状态）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_cache_state (
                session_id      TEXT PRIMARY KEY,
                summary         TEXT DEFAULT '',
                a_start_round   INTEGER DEFAULT 0,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        # ---- 三层记忆架构字段（layer / title / is_active / merged_from / event_date）----
        # layer: 1=原始碎片, 2=事件记忆, 3=核心记忆
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'layer'
                ) THEN
                    ALTER TABLE memories ADD COLUMN layer INTEGER DEFAULT 1;
                END IF;
            END $$;
        """)
        
        # title: 记忆标题（语义锚点，用于搜索加权）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'title'
                ) THEN
                    ALTER TABLE memories ADD COLUMN title TEXT DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # is_active: 是否参与搜索（碎片合并后变为 false）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'is_active'
                ) THEN
                    ALTER TABLE memories ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
                END IF;
            END $$;
        """)
        
        # merged_from: 合并来源的碎片ID列表
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'merged_from'
                ) THEN
                    ALTER TABLE memories ADD COLUMN merged_from INTEGER[] DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # event_date: 事件日期（用于按天整理）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'event_date'
                ) THEN
                    ALTER TABLE memories ADD COLUMN event_date DATE DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # 三层记忆索引
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories (layer);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_active ON memories (is_active);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_event_date ON memories (event_date);
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'entity_scanned'
                ) THEN
                    ALTER TABLE memories ADD COLUMN entity_scanned BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)

        # ---- 实体层：实体、别名、记忆关联 ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id              SERIAL PRIMARY KEY,
                name            TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                entity_type     TEXT NOT NULL DEFAULT 'other',
                description     TEXT DEFAULT '',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_aliases (
                entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                alias           TEXT NOT NULL,
                normalized_alias TEXT NOT NULL UNIQUE,
                PRIMARY KEY (entity_id, normalized_alias)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_entities (
                memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                entity_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                confidence      REAL NOT NULL DEFAULT 1.0,
                source          TEXT NOT NULL DEFAULT 'extractor',
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (memory_id, entity_id)
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities (entity_id);")
        
        # 尝试启用pgvector扩展（向量搜索）
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            HAS_PGVECTOR = True
            print("✅ pgvector扩展已启用")
            
            # 对话表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            
            # 记忆表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            try:
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_embedding 
                    ON memories USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 10);
                """)
            except Exception:
                pass  # ivfflat需要一定行数才能建索引，初期跳过
        except Exception as e:
            HAS_PGVECTOR = False
            print(f"⚠️ pgvector不可用（{e}），向量搜索将使用Python端计算")
            
            # 回退：用TEXT列存JSON格式的向量
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
    
    print("✅ 数据库表结构已就绪")


# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

import jieba
import jieba.analyse

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9]*')
NUM_PATTERN = re.compile(r'\d{2,}')
# 清理查询开头的时间戳（如 "2026-05-02 20:26"）
TIMESTAMP_PATTERN = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*\d{1,2}:\d{1,2}\s*')

# 中文停用词（高频但无搜索价值的词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "和", "与", "也", "都", "又", "就", "但",
    "而", "或", "到", "被", "把", "让", "从", "对", "为", "以",
    "及", "等", "个", "不", "没", "很", "太", "吗", "呢", "吧",
    "啊", "嗯", "哦", "哈", "呀", "嘛", "么", "啦", "哇", "喔",
    "会", "能", "要", "想", "去", "来", "说", "做", "看", "给",
    "上", "下", "里", "中", "大", "小", "多", "少", "好", "可以",
    "什么", "怎么", "如何", "哪里", "哪个", "为什么", "还是",
    "然后", "因为", "所以", "虽然", "但是", "可以", "已经",
    "一个", "一些", "一下", "一点", "一起", "一样",
    "比较", "应该", "可能", "如果", "这个", "那个",
    "自己", "知道", "觉得", "感觉", "时候", "现在",
})

# jieba 用户词典补充（默认词典缺失的词）
for _w in ["手账", "手帐", "搭子", "种草", "拔草", "安利", "内卷", "摆烂", "emo", "网关"]:
    jieba.add_word(_w)


def extract_search_keywords(query: str) -> List[str]:
    """
    从查询中提取搜索关键词（TF-IDF + 正则）

    1. 去掉开头的时间戳噪音
    2. 用 jieba.analyse.extract_tags (TF-IDF) 提取中文关键词
    3. 正则提取英文单词
    4. 保留4位以上数字（年份等，过滤短数字噪音）

    例如：
    "2026-05-02 20:26 写写手账看看书 放松大脑" → ["手账", "放松", "大脑"]
    "我昨天在手机上部署了Render然后吃了晚饭" → ["手机", "部署", "Render", "晚饭"]
    "春节干了什么" → ["春节"]
    "2026除夕"    → ["2026", "除夕"]
    """
    # 去掉时间戳前缀
    cleaned = TIMESTAMP_PATTERN.sub('', query).strip()
    if not cleaned:
        cleaned = query

    keywords = set()

    # 英文单词（2字符以上）
    for match in EN_WORD_PATTERN.finditer(cleaned):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    # 数字串（只保留4位以上，过滤 "05" "20" 这种时间噪音）
    for match in NUM_PATTERN.finditer(cleaned):
        num = match.group()
        if len(num) >= 4:
            keywords.add(num)

    # TF-IDF 关键词提取（比手动分词+停用词好很多）
    tags = jieba.analyse.extract_tags(cleaned, topK=10)
    for tag in tags:
        # 跳过纯英文/数字（已在上面处理）
        if EN_WORD_PATTERN.fullmatch(tag) or NUM_PATTERN.fullmatch(tag):
            continue
        if tag in _STOP_WORDS:
            continue
        keywords.add(tag)

    return list(keywords)


# ============================================================
# 向量搜索（OpenAI 兼容 Embedding API）
# ============================================================

async def compute_embedding(text: str) -> list:
    """调用 OpenAI 兼容的 Embedding API 计算文本向量"""
    if not EMBEDDING_API_KEY:
        return []
    
    try:
        import httpx
        
        if len(text) > 4000:
            text = text[:4000]
        
        body = {
            "model": EMBEDDING_MODEL,
            "input": text,
        }
        # 只有OpenAI官方模型支持dimensions参数，bge-m3等不支持
        if EMBEDDING_DIM > 0 and "text-embedding" in EMBEDDING_MODEL:
            body["dimensions"] = EMBEDDING_DIM
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{EMBEDDING_BASE_URL}/embeddings",
                headers={
                    "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"⚠️ Embedding计算失败: {e}")
        return []


async def save_memory_embedding(conn, memory_id: int, embedding: list):
    """保存记忆向量到memories表"""
    if not embedding:
        return
    
    if HAS_PGVECTOR:
        vec_str = '[' + ','.join(str(f) for f in embedding) + ']'
        await conn.execute(
            "UPDATE memories SET embedding = $1::vector WHERE id = $2",
            vec_str, memory_id
        )
    else:
        import json
        await conn.execute(
            "UPDATE memories SET embedding_json = $1 WHERE id = $2",
            json.dumps(embedding), memory_id
        )


def _cosine_sim(a, b):
    """余弦相似度（纯Python）"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def _min_max_normalize(scores: dict) -> dict:
    """min-max归一化到0-1"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v, max_v = min(vals), max(vals)
    spread = max_v - min_v
    if spread == 0:
        value = 1.0 if max_v > 0 else 0.0
        return {k: value for k in scores}
    return {k: (v - min_v) / spread for k, v in scores.items()}


# ============================================================
# 对话记录操作
# ============================================================

async def save_message(session_id: str, role: str, content: str, model: str = "", metadata: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model, metadata) VALUES ($1, $2, $3, $4, $5)",
            session_id, role, content, model, metadata,
        )


async def get_last_user_content(session_id: str) -> str:
    """获取指定session最后一条user消息的content"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT content FROM conversations
            WHERE session_id = $1 AND role = 'user'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        return row['content'] if row else ""


async def update_last_assistant_message(session_id: str, new_content: str, model: str = ""):
    """覆盖指定session最后一条assistant消息的content（用于re-roll去重）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM conversations
            WHERE session_id = $1 AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        if row:
            await conn.execute(
                "UPDATE conversations SET content = $1, model = $2 WHERE id = $3",
                new_content, model, row['id']
            )
            return True
        return False


async def get_recent_messages(session_id: str, limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content, metadata, created_at FROM conversations WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
            session_id, limit,
        )
        return list(reversed(rows))


async def search_conversations(query: str, limit: int = 20, offset: int = 0):
    """搜索对话内容，返回匹配的session列表"""
    keywords = extract_search_keywords(query)
    if not keywords:
        return [], 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        where_parts = []
        params = []
        for i, kw in enumerate(keywords):
            where_parts.append(f"c.content ILIKE '%' || ${i+1} || '%'")
            params.append(kw)
        where_clause = " OR ".join(where_parts)
        
        count_sql = f"""
            SELECT COUNT(DISTINCT c.session_id) as total
            FROM conversations c
            WHERE {where_clause}
        """
        total_row = await conn.fetchrow(count_sql, *params)
        total = total_row['total'] if total_row else 0
        
        if total == 0:
            return [], 0
        
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        params.extend([limit, offset])
        
        sql = f"""
            WITH matched_sessions AS (
                SELECT DISTINCT c.session_id
                FROM conversations c
                WHERE {where_clause}
            ),
            session_info AS (
                SELECT 
                    ms.session_id,
                    MIN(c.created_at) as first_time,
                    MAX(c.created_at) as last_time,
                    COUNT(*) as message_count
                FROM matched_sessions ms
                JOIN conversations c ON c.session_id = ms.session_id
                GROUP BY ms.session_id
            )
            SELECT 
                si.session_id,
                si.first_time,
                si.last_time,
                si.message_count
            FROM session_info si
            ORDER BY si.last_time DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(sql, *params)
        
        results = []
        for r in rows:
            results.append({
                'session_id': r['session_id'],
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
            })
        
        return results, total


async def update_message_content(message_id: int, new_content: str):
    """更新单条对话消息的内容"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET content = $1 WHERE id = $2",
            new_content, message_id,
        )
        return int(result.split()[-1]) if result else 0


async def delete_single_message(message_id: int):
    """删除单条对话消息（硬删除）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE id = $1",
            message_id,
        )
        return int(result.split()[-1]) if result else 0


# ============================================================
# 记忆操作
# ============================================================

async def save_memory(content: str, importance: int = 5, source_session: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO memories (content, importance, source_session) VALUES ($1, $2, $3) RETURNING id",
            content, importance, source_session,
        )
        
        # MEMORY_VECTOR_ENABLED 时自动计算 embedding
        if MEMORY_VECTOR_ENABLED and row:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding自动计算失败: {e}")
        return row["id"] if row else None


async def _fetch_entity_search_candidates(conn, query: str, keywords: list, limit: int):
    """Return memories reached through an entity name or alias match."""
    normalized_query = normalize_entity_name(query)
    terms = list(dict.fromkeys(
        term for term in [normalized_query, *(normalize_entity_name(k) for k in keywords)] if term
    ))
    if not terms:
        return {}
    rows = await conn.fetch("""
        SELECT m.id, m.content, m.importance, m.created_at, m.layer, m.title,
               e.id AS entity_id, e.name AS entity_name, e.normalized_name,
               e.entity_type, e.description,
               COALESCE(array_agg(DISTINCT ea.alias) FILTER (WHERE ea.alias IS NOT NULL), ARRAY[]::text[]) AS aliases,
               COALESCE(array_agg(DISTINCT ea.normalized_alias) FILTER (WHERE ea.normalized_alias IS NOT NULL), ARRAY[]::text[]) AS normalized_aliases
        FROM entities e
        LEFT JOIN entity_aliases ea ON ea.entity_id = e.id
        JOIN memory_entities me ON me.entity_id = e.id
        JOIN memories m ON m.id = me.memory_id
        WHERE m.is_active = TRUE AND EXISTS (
            SELECT 1 FROM unnest($1::text[]) AS term
            WHERE e.normalized_name LIKE '%' || term || '%'
               OR term LIKE '%' || e.normalized_name || '%'
               OR ea.normalized_alias LIKE '%' || term || '%'
               OR term LIKE '%' || ea.normalized_alias || '%'
        )
        GROUP BY m.id, e.id
        ORDER BY m.importance DESC, m.created_at DESC
        LIMIT $2
    """, terms, max(limit * 10, 30))
    candidates = {}
    for row in rows:
        names = [row["normalized_name"], *(row["normalized_aliases"] or [])]
        exact = any(name in terms for name in names)
        phrase = any(name and name in normalized_query for name in names)
        entity_score = 1.0 if exact else 0.9 if phrase else 0.7
        entity = {
            "id": row["entity_id"], "name": row["entity_name"], "type": row["entity_type"],
            "description": row["description"] or "", "aliases": list(row["aliases"] or []),
        }
        item = candidates.setdefault(row["id"], {
            "content": row["content"], "importance": row["importance"],
            "created_at": row["created_at"], "layer": row["layer"] or 1, "title": row["title"],
            "entity_score": 0.0, "matched_entities": [],
        })
        item["entity_score"] = max(item["entity_score"], entity_score)
        if not any(existing["id"] == entity["id"] for existing in item["matched_entities"]):
            item["matched_entities"].append(entity)
    return candidates


async def _attach_entity_context(conn, results: list):
    """Attach all linked entities while preserving which entities caused recall."""
    if not results:
        return results
    ids = [int(result["id"]) for result in results]
    rows = await conn.fetch("""
        SELECT me.memory_id, me.confidence, e.id, e.name, e.entity_type, e.description,
               COALESCE(array_agg(DISTINCT ea.alias) FILTER (WHERE ea.alias IS NOT NULL), ARRAY[]::text[]) AS aliases
        FROM memory_entities me
        JOIN entities e ON e.id = me.entity_id
        LEFT JOIN entity_aliases ea ON ea.entity_id = e.id
        WHERE me.memory_id = ANY($1::int[])
        GROUP BY me.memory_id, me.confidence, e.id
        ORDER BY me.memory_id, me.confidence DESC, e.name
    """, ids)
    by_memory = {}
    for row in rows:
        by_memory.setdefault(row["memory_id"], []).append({
            "id": row["id"], "name": row["name"], "type": row["entity_type"],
            "description": row["description"] or "", "aliases": list(row["aliases"] or []),
            "confidence": row["confidence"],
        })
    for result in results:
        result["entities"] = by_memory.get(result["id"], [])
        result.setdefault("matched_entities", [])
        result.setdefault("entity_score", 0.0)
    return results


async def search_memories(query: str, limit: int = 10):
    """
    搜索相关记忆
    
    始终启用关键词 + 实体聚合；MEMORY_VECTOR_ENABLED=true 时再加入向量召回。
    """
    if MEMORY_VECTOR_ENABLED:
        return await search_memories_hybrid(query, limit)
    
    # ---- 纯关键词搜索 ----
    keywords = extract_search_keywords(query)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not keywords:
            entity_candidates = await _fetch_entity_search_candidates(conn, query, [], limit)
            now = datetime.now(dt_timezone.utc)
            results = []
            for memory_id, info in entity_candidates.items():
                days = max(0.0, (now - info["created_at"]).total_seconds() / 86400.0)
                results.append({
                    "id": memory_id, "content": info["content"], "importance": info["importance"],
                    "created_at": info["created_at"], "hit_count": 0,
                    "layer": info["layer"], "title": info["title"],
                    "entity_score": info["entity_score"], "matched_entities": info["matched_entities"],
                    "score": (WEIGHT_IMPORTANCE * info["importance"] / 10.0 +
                              WEIGHT_RECENCY / (1.0 + days) + MEMORY_HW_ENTITY * info["entity_score"]),
                })
            results.sort(key=lambda row: (-row["score"], -row["importance"]))
            results = results[:limit]
            await _attach_entity_context(conn, results)
            if results:
                await conn.execute(
                    "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                    [row["id"] for row in results],
                )
            return results

        # 每个关键词命中得1分
        case_parts = []
        params = []
        for i, kw in enumerate(keywords):
            case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
            params.append(kw)
        
        hit_count_expr = " + ".join(case_parts)
        max_hits = len(keywords)
        
        # 至少命中一个关键词（只搜索活跃记忆）
        where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
        where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"
        
        limit_idx = len(keywords) + 1
        params.append(limit * 3)
        
        sql = f"""
            SELECT 
                id, content, importance, created_at, layer, title,
                ({hit_count_expr}) AS hit_count,
                (
                    {WEIGHT_KEYWORD} * ({hit_count_expr})::float / {max_hits}.0 +
                    {WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                    {WEIGHT_RECENCY} * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0))
                ) AS score
            FROM memories
            WHERE {where_clause}
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT ${limit_idx}
        """
        
        keyword_rows = await conn.fetch(sql, *params)
        candidates = {row["id"]: dict(row) for row in keyword_rows}
        entity_candidates = await _fetch_entity_search_candidates(conn, query, keywords, limit)
        now = datetime.now(dt_timezone.utc)
        for memory_id, entity_info in entity_candidates.items():
            if memory_id in candidates:
                candidates[memory_id]["score"] = min(
                    1.5, float(candidates[memory_id]["score"]) + MEMORY_HW_ENTITY * entity_info["entity_score"]
                )
            else:
                days = max(0.0, (now - entity_info["created_at"]).total_seconds() / 86400.0)
                candidates[memory_id] = {
                    "id": memory_id, "content": entity_info["content"],
                    "importance": entity_info["importance"], "created_at": entity_info["created_at"],
                    "layer": entity_info["layer"], "title": entity_info["title"],
                    "hit_count": 0,
                    "score": (WEIGHT_IMPORTANCE * entity_info["importance"] / 10.0 +
                              WEIGHT_RECENCY * (1.0 / (1.0 + days)) +
                              MEMORY_HW_ENTITY * entity_info["entity_score"]),
                }
            candidates[memory_id]["entity_score"] = entity_info["entity_score"]
            candidates[memory_id]["matched_entities"] = entity_info["matched_entities"]

        results = sorted(candidates.values(), key=lambda row: (-float(row["score"]), -row["importance"]))
        before_count = len(results)
        if MIN_SCORE_THRESHOLD > 0:
            results = [row for row in results if float(row["score"]) >= MIN_SCORE_THRESHOLD]
        filtered = before_count - len(results)
        results = results[:limit]
        await _attach_entity_context(conn, results)
        
        if results:
            print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]}{'...' if len(keywords)>8 else ''} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 条低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (hits={r['hit_count']}, imp={r['importance']}) {r['content'][:60]}...")
            
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 搜索 '{query}' → 关键词 {keywords[:8]} → 无结果" + (f"（{filtered} 条被分数阈值过滤）" if filtered else ""))
        
        return results


async def search_memories_hybrid(query: str, limit: int = 10):
    """
    记忆聚合搜索：关键词 + 向量 + 实体，结合重要度与时间衰减排序。
    
    实体命中使用 MEMORY_HW_ENTITY 作为现有综合分数之外的加成。
    """
    from datetime import datetime, timezone
    
    keywords = extract_search_keywords(query)
    query_embedding = await compute_embedding(query) if EMBEDDING_API_KEY else []
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidates = {}  # id -> {content, importance, created_at, kw_score, similarity}
        
        # ---- 关键词路 ----
        if keywords:
            case_parts = []
            params = []
            for i, kw in enumerate(keywords):
                case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
                params.append(kw)
            
            hit_count_expr = " + ".join(case_parts)
            max_hits = len(keywords)
            where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
            where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"
            
            limit_idx = len(keywords) + 1
            params.append(limit * 3)
            
            kw_sql = f"""
                SELECT id, content, importance, created_at, layer, title,
                       ({hit_count_expr}) AS hit_count,
                       ({hit_count_expr})::float / {max_hits}.0 AS kw_score
                FROM memories
                WHERE {where_clause}
                ORDER BY kw_score DESC
                LIMIT ${limit_idx}
            """
            kw_rows = await conn.fetch(kw_sql, *params)
            
            for r in kw_rows:
                candidates[r['id']] = {
                    'content': r['content'],
                    'importance': r['importance'],
                    'created_at': r['created_at'],
                    'layer': r['layer'] or 1,
                    'title': r['title'],
                    'hit_count': r['hit_count'],
                    'kw_score': float(r['kw_score']),
                    'similarity': 0.0,
                }
        
        # ---- 向量路 ----
        if query_embedding:
            if HAS_PGVECTOR:
                vec_str = '[' + ','.join(str(f) for f in query_embedding) + ']'
                sem_rows = await conn.fetch("""
                    SELECT id, content, importance, created_at, layer, title,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM memories
                    WHERE embedding IS NOT NULL AND is_active = TRUE
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                """, vec_str, limit * 3)
            else:
                # Python端计算cosine
                import json
                all_mem = await conn.fetch("""
                    SELECT id, content, importance, created_at, layer, title, embedding_json
                    FROM memories WHERE embedding_json IS NOT NULL AND is_active = TRUE
                """)
                
                scored = []
                for row in all_mem:
                    try:
                        emb = json.loads(row['embedding_json'])
                        sim = _cosine_sim(query_embedding, emb)
                        scored.append({**dict(row), 'similarity': sim})
                    except Exception:
                        continue
                scored.sort(key=lambda x: -x['similarity'])
                sem_rows = scored[:limit * 3]
            
            for r in sem_rows:
                sim = float(r['similarity'])
                if sim < MEMORY_SEMANTIC_THRESHOLD:
                    continue
                mid = r['id']
                if mid in candidates:
                    candidates[mid]['similarity'] = sim
                else:
                    candidates[mid] = {
                        'content': r['content'],
                        'importance': r['importance'],
                        'created_at': r['created_at'],
                        'layer': r['layer'] or 1,
                        'title': r['title'],
                        'hit_count': 0,
                        'kw_score': 0.0,
                        'similarity': sim,
                    }
            
            # debug：向量路统计
            sem_total = len(sem_rows)
            sem_passed = sum(1 for r in sem_rows if float(r['similarity']) >= MEMORY_SEMANTIC_THRESHOLD)
            sem_max = max((float(r['similarity']) for r in sem_rows), default=0)
            if sem_total > 0 and sem_passed == 0:
                print(f"   🔢 向量路: {sem_total}条候选全被阈值过滤（最高sim={sem_max:.3f}, 阈值={MEMORY_SEMANTIC_THRESHOLD}）")
            elif sem_total > 0:
                print(f"   🔢 向量路: {sem_passed}/{sem_total}条通过阈值（最高sim={sem_max:.3f}）")

        # ---- 实体路：正式名/别名命中后召回该实体的所有活跃记忆 ----
        entity_candidates = await _fetch_entity_search_candidates(conn, query, keywords, limit)
        for memory_id, entity_info in entity_candidates.items():
            if memory_id in candidates:
                candidates[memory_id]["entity_score"] = entity_info["entity_score"]
                candidates[memory_id]["matched_entities"] = entity_info["matched_entities"]
            else:
                candidates[memory_id] = {
                    "content": entity_info["content"],
                    "importance": entity_info["importance"],
                    "created_at": entity_info["created_at"],
                    "layer": entity_info["layer"],
                    "title": entity_info["title"],
                    "hit_count": 0,
                    "kw_score": 0.0,
                    "similarity": 0.0,
                    "entity_score": entity_info["entity_score"],
                    "matched_entities": entity_info["matched_entities"],
                }
        
        if not candidates:
            print(f"🔍 聚合搜索 '{query}' → 关键词、向量、实体三路均无结果")
            return []
        
        # ---- 归一化 + 加权 ----
        kw_norm = _min_max_normalize({mid: v['kw_score'] for mid, v in candidates.items()})
        sem_norm = _min_max_normalize({mid: v['similarity'] for mid, v in candidates.items()})
        entity_norm = _min_max_normalize({mid: v.get('entity_score', 0.0) for mid, v in candidates.items()})
        
        now = datetime.now(timezone.utc)
        final = []
        for mid, info in candidates.items():
            kw = kw_norm.get(mid, 0.0)
            sem = sem_norm.get(mid, 0.0)
            entity_score = entity_norm.get(mid, 0.0)
            imp = info['importance'] / 10.0
            days = (now - info['created_at']).total_seconds() / 86400.0
            rec = 1.0 / (1.0 + days)
            
            score = (MEMORY_HW_KEYWORD * kw +
                     MEMORY_HW_SEMANTIC * sem +
                     MEMORY_HW_IMPORTANCE * imp +
                     MEMORY_HW_RECENCY * rec +
                     MEMORY_HW_ENTITY * entity_score)
            
            final.append({
                'id': mid,
                'content': info['content'],
                'importance': info['importance'],
                'created_at': info['created_at'],
                'layer': info.get('layer', 1),
                'title': info.get('title'),
                'hit_count': info['hit_count'],
                'similarity': info['similarity'],
                'entity_score': info.get('entity_score', 0.0),
                'matched_entities': info.get('matched_entities', []),
                'score': score,
            })
        
        final.sort(key=lambda x: (-x['score'], -x['importance']))
        
        # 过滤低分
        if MIN_SCORE_THRESHOLD > 0:
            before_count = len(final)
            final = [r for r in final if r['score'] >= MIN_SCORE_THRESHOLD]
            filtered = before_count - len(final)
        else:
            filtered = 0
        
        results = final[:limit]
        await _attach_entity_context(conn, results)
        
        if results:
            mode_tag = "混合" if query_embedding else "关键词"
            kw_tag = f"关键词 {keywords[:6]}" if keywords else "无关键词"
            print(f"🔍 {mode_tag}搜索 '{query}' → {kw_tag} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 条低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (kw={r['hit_count']}, sim={r['similarity']:.2f}, entity={r['entity_score']:.2f}, imp={r['importance']}) {r['content'][:60]}...")
            
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 混合搜索 '{query}' → 无结果" + (f"（{filtered} 条被过滤）" if filtered else ""))
        
        return [dict(r) for r in results]


async def get_pending_memory_embedding_count():
    """查询还没有embedding的记忆数量"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL"
            )
        else:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL"
            )


async def backfill_memory_embeddings(batch_size: int = 20):
    """给已有记忆补算embedding（没有embedding的记忆）"""
    if not EMBEDDING_API_KEY:
        print("⚠️ EMBEDDING_API_KEY 未设置，无法补算embedding")
        return 0
    
    pool = await get_pool()
    total_updated = 0
    
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
        else:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding_json IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
    
    if not rows:
        print("✅ 所有记忆已有embedding，无需补算")
        return 0
    
    print(f"🔄 开始补算记忆embedding... 本批 {len(rows)} 条")
    
    async with pool.acquire() as conn:
        for row in rows:
            try:
                embedding = await compute_embedding(row['content'] or '')
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
                    total_updated += 1
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding计算失败: {e}")
    
    # 检查剩余
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL")
        else:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL")
    
    print(f"✅ 本批补算完成：{total_updated}/{len(rows)} 条成功" + (f"，剩余 {remaining} 条待处理" if remaining > 0 else ""))
    return total_updated


async def get_recent_memories(limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, content, importance, created_at FROM memories ORDER BY created_at DESC LIMIT $1",
            limit,
        )


async def get_all_memories_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    """导出所有记忆（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, importance, source_session, created_at FROM memories ORDER BY id"
        )
        return [dict(r) for r in rows]


async def get_all_memories_detail(limit: int = None, layer: int = None, active_only: bool = None):
    """获取所有记忆（含 id，用于管理页面）
    
    Args:
        limit: 可选，限制返回数量
        layer: 可选，筛选指定层级（1=原始碎片, 2=事件记忆, 3=核心记忆）
        active_only: 可选，是否只返回 is_active=true 的记忆
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1
        
        if layer is not None:
            conditions.append(f"layer = ${param_idx}")
            params.append(layer)
            param_idx += 1
        
        if active_only is not None:
            conditions.append(f"is_active = ${param_idx}")
            params.append(active_only)
            param_idx += 1
        
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        
        if limit is not None:
            limit_clause = f"LIMIT ${param_idx}"
            params.append(limit)
        else:
            limit_clause = ""
        
        rows = await conn.fetch(f"""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, merged_from, event_date
            FROM memories
            {where_clause}
            ORDER BY id
            {limit_clause}
        """, *params)
        return [dict(r) for r in rows]


async def update_memory(memory_id: int, content: str = None, importance: int = None):
    """更新单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if content is not None and importance is not None:
            await conn.execute(
                "UPDATE memories SET content = $1, importance = $2 WHERE id = $3",
                content, importance, memory_id
            )
        elif content is not None:
            await conn.execute(
                "UPDATE memories SET content = $1 WHERE id = $2",
                content, memory_id
            )
        elif importance is not None:
            await conn.execute(
                "UPDATE memories SET importance = $1 WHERE id = $2",
                importance, memory_id
            )


async def delete_memory(memory_id: int):
    """删除单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)


async def delete_memories_batch(memory_ids: list):
    """批量删除记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE id = ANY($1::int[])", memory_ids
        )


# ============================================================
# 网关配置
# ============================================================

async def get_gateway_config(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM gateway_config WHERE key = $1", key)
        return row['value'] if row else default


async def set_gateway_config(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO gateway_config (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, value)


async def get_all_gateway_config() -> dict:
    """获取所有配置项"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM gateway_config")
        return {r['key']: r['value'] for r in rows}


# ============================================================
# 对话历史读取（分区缓存用）
# ============================================================

async def get_conversation_messages(session_id: str, limit: int = 100):
    """按时间正序读取session的消息"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content, metadata, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at ASC
            LIMIT $2
        """, session_id, limit)
        return [dict(r) for r in rows]


# ============================================================
# 分区缓存状态管理
# ============================================================

async def get_session_cache_state(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, a_start_round, updated_at FROM session_cache_state WHERE session_id = $1",
            session_id
        )
        if row:
            raw_summary = row['summary'] or ''
            summary_parts = []
            if raw_summary:
                try:
                    import json
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, list):
                        summary_parts = parsed
                    else:
                        summary_parts = [raw_summary]
                except (json.JSONDecodeError, ValueError):
                    summary_parts = [raw_summary]
            return {
                'summary_parts': summary_parts,
                'a_start_round': row['a_start_round'] or 0,
                'updated_at': row['updated_at'],
            }
        return {'summary_parts': [], 'a_start_round': 0, 'updated_at': None}


async def save_session_cache_state(session_id: str, summary_parts: list, a_start_round: int):
    import json
    summary_json = json.dumps(summary_parts, ensure_ascii=False)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO session_cache_state (session_id, summary, a_start_round, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (session_id) 
            DO UPDATE SET summary = $2, a_start_round = $3, updated_at = NOW()
        """, session_id, summary_json, a_start_round)


# ============================================================
# Token 使用记录
# ============================================================

async def ensure_token_usage_table():
    """确保token_usage表存在（在init_tables里调用）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT,
                model           TEXT,
                prompt_tokens   INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                usage_type      TEXT DEFAULT 'chat',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at DESC);
        """)


async def save_token_usage(session_id: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int, usage_type: str = "chat"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO token_usage (session_id, model, prompt_tokens, completion_tokens, total_tokens, usage_type)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, session_id, model, prompt_tokens, completion_tokens, total_tokens, usage_type)


# ============================================================
# 对话记录管理
# ============================================================

async def get_conversations_paginated(page: int = 1, per_page: int = 20):
    offset = (page - 1) * per_page
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(
            "SELECT COUNT(DISTINCT session_id) as total FROM conversations"
        )
        total = total_row['total'] if total_row else 0

        rows = await conn.fetch("""
            WITH session_info AS (
                SELECT session_id, MIN(created_at) as first_time, MAX(created_at) as last_time, COUNT(*) as message_count
                FROM conversations GROUP BY session_id ORDER BY last_time DESC LIMIT $1 OFFSET $2
            )
            SELECT si.*,
                   COALESCE(tu.total_all, 0) as total_tokens
            FROM session_info si
            LEFT JOIN (
                SELECT session_id, SUM(total_tokens) as total_all FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id
            ) tu ON si.session_id = tu.session_id
            ORDER BY si.last_time DESC
        """, per_page, offset)
        
        results = []
        for r in rows:
            preview_row = await conn.fetchrow(
                "SELECT content FROM conversations WHERE session_id = $1 AND role = 'user' ORDER BY created_at LIMIT 1",
                r['session_id']
            )
            preview = preview_row['content'][:80] if preview_row else ''
            title = (preview[:30] + '...' if len(preview) > 30 else preview) or r['session_id']
            results.append({
                'session_id': r['session_id'],
                'title': title,
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
                'preview': preview,
                'total_tokens': r['total_tokens'],
            })
        return results, total


async def delete_conversation(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def batch_delete_conversations(session_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = ANY($1)", session_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", session_ids)


async def merge_sessions_to_target(source_ids: list, target_id: str) -> dict:
    if not source_ids:
        return {'merged_sessions': 0, 'merged_messages': 0, 'merged_token_records': 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE conversations SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        token_count = await conn.fetchval("SELECT COUNT(*) FROM token_usage WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE token_usage SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", source_ids)
        return {'merged_sessions': len(source_ids), 'merged_messages': msg_count or 0, 'merged_token_records': token_count or 0}


async def list_all_session_cache_states() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT scs.session_id, scs.summary, scs.a_start_round, scs.updated_at,
                   COALESCE(c.message_count, 0) as message_count,
                   COALESCE(tu.chat_tokens, 0) as chat_tokens
            FROM session_cache_state scs
            LEFT JOIN (SELECT session_id, COUNT(*) as message_count FROM conversations GROUP BY session_id) c ON scs.session_id = c.session_id
            LEFT JOIN (SELECT session_id, SUM(total_tokens) as chat_tokens FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id) tu ON scs.session_id = tu.session_id
            ORDER BY scs.updated_at DESC
        """)
        results = []
        for r in rows:
            raw_summary = r['summary'] or ''
            try:
                import json
                parsed = json.loads(raw_summary)
                if isinstance(parsed, list):
                    summary_parts = parsed
                else:
                    summary_parts = [raw_summary] if raw_summary else []
            except (json.JSONDecodeError, ValueError):
                summary_parts = [raw_summary] if raw_summary else []
            results.append({
                'session_id': r['session_id'],
                'summary': '\n\n'.join(summary_parts),
                'summary_length': sum(len(p) for p in summary_parts),
                'summary_count': len(summary_parts),
                'a_start_round': r['a_start_round'],
                'updated_at': r['updated_at'].isoformat() if r['updated_at'] else None,
                'message_count': r['message_count'],
                'chat_tokens': r['chat_tokens'],
            })
        return results


async def delete_session_cache_state(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def rename_session_id(old_id: str, new_id: str) -> bool:
    """重命名对话线ID（事务内同时修改三个表）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 检查新ID是否已存在
            exists = await conn.fetchval(
                "SELECT 1 FROM session_cache_state WHERE session_id = $1", new_id
            )
            if exists:
                return False
            # session_cache_state
            await conn.execute(
                "UPDATE session_cache_state SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # conversations
            await conn.execute(
                "UPDATE conversations SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # token_usage
            await conn.execute(
                "UPDATE token_usage SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            return True


def db_row_to_message(row: dict) -> dict:
    """
    把DB记录还原成API消息格式。
    
    普通消息: {"role": "user", "content": "你好"} 
    工具调用: {"role": "assistant", "content": null, "tool_calls": [...]}
    工具结果: {"role": "tool", "content": "结果", "tool_call_id": "call_xxx"}
    思维链:   {"role": "assistant", "content": "回答", "reasoning_content": "思维链"}
    """
    import json as _json
    msg = {"role": row["role"], "content": row.get("content") or ""}
    
    meta_str = row.get("metadata")
    if meta_str:
        try:
            meta = _json.loads(meta_str)
            # assistant 带 tool_calls
            if "tool_calls" in meta:
                msg["tool_calls"] = meta["tool_calls"]
                if not row.get("content"):
                    msg["content"] = None
            # assistant 带 reasoning_content（deepseek thinking mode）
            if "reasoning_content" in meta:
                msg["reasoning_content"] = meta["reasoning_content"]
            # tool 消息带 tool_call_id
            if "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            # 其他可能的字段（name 等）
            if "name" in meta:
                msg["name"] = meta["name"]
        except Exception:
            pass
    
    return msg


async def export_all_conversations():
    """导出所有对话记录（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT session_id, role, content, model, created_at
            FROM conversations
            ORDER BY session_id, created_at
        """)
        return [
            {
                'session_id': r['session_id'],
                'role': r['role'],
                'content': r['content'],
                'model': r['model'] or '',
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            }
            for r in rows
        ]


async def import_conversations(records: list):
    """
    导入对话记录（自动去重）
    
    records: [{ session_id, role, content, model?, created_at? }, ...]
    按 session_id + role + created_at 三元组去重，已存在的跳过。
    返回 (导入数量, 跳过数量)
    """
    if not records:
        return 0, 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        imported = 0
        skipped = 0
        for r in records:
            session_id = r.get('session_id')
            role = r.get('role')
            content = r.get('content')
            
            if not all([session_id, role, content]):
                continue
            
            model = r.get('model', '')
            created_at = r.get('created_at')
            
            # 解析时间
            from datetime import datetime
            if created_at and isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except:
                    created_at = None
            
            # 去重检查
            if created_at:
                existing = await conn.fetchrow("""
                    SELECT id FROM conversations
                    WHERE session_id = $1 AND role = $2 AND created_at = $3
                    LIMIT 1
                """, session_id, role, created_at)
                
                if existing:
                    skipped += 1
                    continue
                
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                """, session_id, role, content, model, created_at)
            else:
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model)
                    VALUES ($1, $2, $3, $4)
                """, session_id, role, content, model)
            
            imported += 1
        
        if skipped:
            print(f"📥 导入对话: {imported} 条新增, {skipped} 条已存在跳过")
        else:
            print(f"📥 导入对话: {imported} 条新增")
        
        return imported, skipped


# ============================================================
# 三层记忆架构（碎片/事件/核心）
# ============================================================

async def get_fragments_by_date(event_date):
    """获取指定日期的原始碎片（用于每日整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(event_date.year, event_date.month, event_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def get_fragments_by_date_range(start_date, end_date):
    """获取指定时间段的原始碎片（用于跨天整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(start_date.year, start_date.month, start_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    # end_date 当天结束 = end_date 下一天的 00:00
    end_utc = datetime(end_date.year, end_date.month, end_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc) + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def create_event_memory(title: str, content: str, importance: int, 
                               event_date, merged_from: list):
    """创建事件记忆（从碎片合并而来）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, 2, $3, TRUE, $4, $5)
            RETURNING id
        """, content, importance, title, merged_from, event_date)
        
        new_id = row['id'] if row else None

        if new_id and merged_from:
            await conn.execute("""
                INSERT INTO memory_entities (memory_id, entity_id, confidence, source)
                SELECT $1, entity_id, MAX(confidence), 'inherited'
                FROM memory_entities WHERE memory_id = ANY($2::int[])
                GROUP BY entity_id
                ON CONFLICT (memory_id, entity_id) DO UPDATE SET
                    confidence = GREATEST(memory_entities.confidence, EXCLUDED.confidence)
            """, new_id, merged_from)
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 事件记忆embedding计算失败（id={new_id}）: {e}")
        
        return new_id


async def deactivate_memories(memory_ids: list):
    """将记忆标记为不活跃（合并后的碎片）"""
    if not memory_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memories SET is_active = FALSE
            WHERE id = ANY($1::int[])
        """, memory_ids)


async def promote_to_core(memory_id: int, title: str = None):
    """将记忆升级为核心记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if title:
            await conn.execute("""
                UPDATE memories SET layer = 3, title = $2
                WHERE id = $1
            """, memory_id, title)
        else:
            await conn.execute("""
                UPDATE memories SET layer = 3
                WHERE id = $1
            """, memory_id)


async def merge_memories(memory_ids: list, new_title: str, new_content: str, 
                         importance: int, layer: int = 2):
    """合并多条记忆为一条新记忆"""
    if not memory_ids:
        return None
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取原记忆的日期（取最早的）
        rows = await conn.fetch("""
            SELECT MIN(DATE(created_at)) as event_date
            FROM memories WHERE id = ANY($1::int[])
        """, memory_ids)
        event_date = rows[0]['event_date'] if rows else None
        
        # 创建新记忆
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, $3, $4, TRUE, $5, $6)
            RETURNING id
        """, new_content, importance, layer, new_title, memory_ids, event_date)
        
        new_id = row['id'] if row else None

        if new_id:
            await conn.execute("""
                INSERT INTO memory_entities (memory_id, entity_id, confidence, source)
                SELECT $1, entity_id, MAX(confidence), 'inherited'
                FROM memory_entities WHERE memory_id = ANY($2::int[])
                GROUP BY entity_id
                ON CONFLICT (memory_id, entity_id) DO UPDATE SET
                    confidence = GREATEST(memory_entities.confidence, EXCLUDED.confidence)
            """, new_id, memory_ids)
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(new_content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 合并记忆embedding计算失败（id={new_id}）: {e}")
        
        # 将原记忆标记为不活跃
        if new_id:
            await deactivate_memories(memory_ids)
        
        return new_id


async def check_duplicate_memory(new_content: str, threshold: float = 0.7) -> dict:
    """检查新记忆是否与现有记忆重复
    
    三层去重策略：
    1. 精确匹配：内容完全相同
    2. 包含关系：新内容包含旧内容，或旧内容包含新内容
    3. 关键词重叠度：Jaccard 相似度 > threshold
    
    Returns:
        {
            "is_duplicate": bool,
            "reason": str,  # "exact" / "containment" / "similarity"
            "matched_id": int or None,
            "similarity": float or None
        }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取所有活跃记忆
        rows = await conn.fetch("""
            SELECT id, content FROM memories 
            WHERE is_active = TRUE
        """)
        
        new_content_lower = new_content.strip().lower()
        new_keywords = set(extract_search_keywords(new_content))
        
        for row in rows:
            old_content = row['content']
            old_content_lower = old_content.strip().lower()
            
            # 第一层：精确匹配
            if new_content_lower == old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "exact",
                    "matched_id": row['id'],
                    "similarity": 1.0
                }
            
            # 第二层：包含关系
            if new_content_lower in old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment",
                    "matched_id": row['id'],
                    "similarity": len(new_content) / len(old_content)
                }
            if old_content_lower in new_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment_update",
                    "matched_id": row['id'],
                    "similarity": len(old_content) / len(new_content)
                }
            
            # 第三层：关键词重叠度（Jaccard 相似度）
            old_keywords = set(extract_search_keywords(old_content))
            if new_keywords and old_keywords:
                intersection = new_keywords & old_keywords
                union = new_keywords | old_keywords
                similarity = len(intersection) / len(union) if union else 0
                
                if similarity > threshold:
                    return {
                        "is_duplicate": True,
                        "reason": "similarity",
                        "matched_id": row['id'],
                        "similarity": similarity
                    }
        
        return {
            "is_duplicate": False,
            "reason": None,
            "matched_id": None,
            "similarity": None
        }


async def update_memory_with_layer(memory_id: int, content: str = None, 
                                    importance: int = None, title: str = None,
                                    layer: int = None, is_active: bool = None):
    """更新记忆（支持三层架构新字段）"""
    updates = []
    params = []
    param_idx = 2  # $1 给 memory_id
    
    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1
    
    if importance is not None:
        updates.append(f"importance = ${param_idx}")
        params.append(importance)
        param_idx += 1
    
    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1
    
    if layer is not None:
        updates.append(f"layer = ${param_idx}")
        params.append(layer)
        param_idx += 1
    
    if is_active is not None:
        updates.append(f"is_active = ${param_idx}")
        params.append(is_active)
        param_idx += 1
    
    if not updates:
        return
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = $1",
            memory_id, *params
        )


async def get_layer_statistics():
    """获取各层记忆的统计数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                layer,
                COUNT(*) as count,
                COUNT(*) FILTER (WHERE is_active = TRUE) as active_count
            FROM memories
            GROUP BY layer
            ORDER BY layer
        """)
        
        stats = {
            "layer_1": {"total": 0, "active": 0},  # 原始碎片
            "layer_2": {"total": 0, "active": 0},  # 事件记忆
            "layer_3": {"total": 0, "active": 0},  # 核心记忆
        }
        
        for row in rows:
            layer = row['layer'] or 1  # 默认为层级1
            key = f"layer_{layer}"
            if key in stats:
                stats[key] = {
                    "total": row['count'],
                    "active": row['active_count']
                }
        
        return stats


async def cleanup_old_fragments(days: int = 30):
    """清理指定天数前的归档碎片
    
    只清理满足以下条件的记忆：
    - layer = 1（原始碎片）
    - is_active = FALSE（已归档）
    - created_at 在 days 天之前
    
    Returns:
        删除的记忆数量
    """
    from datetime import datetime, timedelta
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        cutoff_date = datetime.now() - timedelta(days=days)
        
        result = await conn.execute("""
            DELETE FROM memories
            WHERE layer = 1
            AND is_active = FALSE
            AND created_at < $1
        """, cutoff_date)
        
        # 解析删除数量，格式如 "DELETE 5"
        deleted = int(result.split()[-1]) if result else 0
        return deleted


async def revert_merge(memory_id: int):
    """撤回合并操作
    
    恢复原始碎片（is_active = TRUE），删除合并后的事件记忆
    
    Args:
        memory_id: 要撤回的事件记忆ID
        
    Returns:
        {"status": "ok", "restored": 恢复的碎片数量}
        或 {"error": "错误信息"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取事件记忆信息
        row = await conn.fetchrow("""
            SELECT id, layer, merged_from FROM memories WHERE id = $1
        """, memory_id)
        
        if not row:
            return {"error": "记忆不存在"}
        
        if row['layer'] != 2:
            return {"error": "只能撤回事件记忆的合并"}
        
        merged_from = row['merged_from']
        if not merged_from or len(merged_from) == 0:
            return {"error": "没有合并来源，无法撤回"}
        
        # 恢复原始碎片
        result = await conn.execute("""
            UPDATE memories SET is_active = TRUE
            WHERE id = ANY($1::int[])
        """, merged_from)
        restored = int(result.split()[-1]) if result else 0
        
        # 删除事件记忆
        await conn.execute("""
            DELETE FROM memories WHERE id = $1
        """, memory_id)
        
        return {"status": "ok", "restored": restored}


# ============================================================
# 实体层
# ============================================================

def normalize_entity_name(name: str) -> str:
    """Generate the canonical lookup key without changing the display name."""
    return re.sub(r"\s+", " ", str(name or "").strip()).casefold()


async def link_memory_entities(memory_id: int, entities: list, source: str = "extractor"):
    """Upsert extracted entities and link them to one memory."""
    if not memory_id or not entities:
        return 0
    pool = await get_pool()
    linked = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in entities:
                if isinstance(item, str):
                    item = {"name": item, "type": "other"}
                name = str(item.get("name", "")).strip()
                normalized = normalize_entity_name(name)
                if not normalized:
                    continue
                entity_type = str(item.get("type", "other") or "other").strip().lower()
                allowed_types = {"person", "place", "organization", "project", "object", "pet", "activity", "event", "other"}
                if entity_type not in allowed_types:
                    entity_type = "other"
                try:
                    confidence = max(0.0, min(1.0, float(item.get("confidence", 1.0))))
                except (TypeError, ValueError):
                    confidence = 1.0
                row = await conn.fetchrow("""
                    INSERT INTO entities (name, normalized_name, entity_type)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (normalized_name) DO UPDATE SET
                        entity_type = CASE WHEN entities.entity_type = 'other' THEN EXCLUDED.entity_type ELSE entities.entity_type END,
                        updated_at = NOW()
                    RETURNING id
                """, name, normalized, entity_type)
                await conn.execute("""
                    INSERT INTO memory_entities (memory_id, entity_id, confidence, source)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (memory_id, entity_id) DO UPDATE SET
                        confidence = GREATEST(memory_entities.confidence, EXCLUDED.confidence),
                        source = EXCLUDED.source
                """, memory_id, row["id"], confidence, source)
                linked += 1
    return linked


async def get_entities_for_memory_ids(memory_ids: list) -> dict:
    if not memory_ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT me.memory_id, e.id, e.name, e.entity_type, me.confidence
            FROM memory_entities me
            JOIN entities e ON e.id = me.entity_id
            WHERE me.memory_id = ANY($1::int[])
            ORDER BY me.memory_id, me.confidence DESC, e.name
        """, [int(memory_id) for memory_id in memory_ids])
    result = {}
    for row in rows:
        result.setdefault(row["memory_id"], []).append({
            "id": row["id"], "name": row["name"], "type": row["entity_type"],
            "confidence": row["confidence"],
        })
    return result


async def list_entities():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT e.id, e.name, e.entity_type, e.description, e.created_at, e.updated_at,
                   COUNT(DISTINCT me.memory_id)::int AS memory_count,
                   COALESCE(array_agg(DISTINCT ea.alias) FILTER (WHERE ea.alias IS NOT NULL), ARRAY[]::text[]) AS aliases
            FROM entities e
            LEFT JOIN memory_entities me ON me.entity_id = e.id
            LEFT JOIN entity_aliases ea ON ea.entity_id = e.id
            GROUP BY e.id
            ORDER BY memory_count DESC, e.name
        """)
        return [dict(row) for row in rows]


async def get_entity_memories(entity_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT m.id, m.content, m.importance, m.source_session, m.created_at,
                   m.layer, m.title, m.is_active, me.confidence
            FROM memory_entities me
            JOIN memories m ON m.id = me.memory_id
            WHERE me.entity_id = $1
            ORDER BY m.created_at DESC
        """, entity_id)
        return [dict(row) for row in rows]


async def get_unlinked_memories(limit: int = 30):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT m.id, m.content
            FROM memories m
            WHERE m.is_active = TRUE AND COALESCE(m.entity_scanned, FALSE) = FALSE
              AND NOT EXISTS (SELECT 1 FROM memory_entities me WHERE me.memory_id = m.id)
            ORDER BY m.id
            LIMIT $1
        """, limit)
        return [dict(row) for row in rows]


async def mark_memories_entity_scanned(memory_ids: list):
    if not memory_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET entity_scanned = TRUE WHERE id = ANY($1::int[])",
            [int(memory_id) for memory_id in memory_ids],
        )


async def merge_entities(source_id: int, target_id: int):
    if source_id == target_id:
        return {"error": "源实体和目标实体不能相同"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            source = await conn.fetchrow("SELECT id, name, normalized_name FROM entities WHERE id = $1", source_id)
            target = await conn.fetchrow("SELECT id FROM entities WHERE id = $1", target_id)
            if not source or not target:
                return {"error": "实体不存在"}
            await conn.execute("""
                INSERT INTO memory_entities (memory_id, entity_id, confidence, source)
                SELECT memory_id, $2, confidence, source FROM memory_entities WHERE entity_id = $1
                ON CONFLICT (memory_id, entity_id) DO UPDATE SET
                    confidence = GREATEST(memory_entities.confidence, EXCLUDED.confidence)
            """, source_id, target_id)
            await conn.execute("""
                INSERT INTO entity_aliases (entity_id, alias, normalized_alias)
                SELECT $2, alias, normalized_alias FROM entity_aliases WHERE entity_id = $1
                ON CONFLICT (normalized_alias) DO NOTHING
            """, source_id, target_id)
            await conn.execute("""
                INSERT INTO entity_aliases (entity_id, alias, normalized_alias)
                VALUES ($1, $2, $3) ON CONFLICT (normalized_alias) DO NOTHING
            """, target_id, source["name"], source["normalized_name"])
            await conn.execute("DELETE FROM entities WHERE id = $1", source_id)
    return {"status": "ok", "source_id": source_id, "target_id": target_id}
