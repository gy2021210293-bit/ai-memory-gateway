"""
预置记忆导入示例
================
把你想让 AI 从一开始就"记住"的事情写在这里。
部署后访问 /import/seed-memories 即可一次性导入。

使用方法：
1. 复制此文件为 seed_memories.py
2. 修改 SEED_MEMORIES 列表
3. 部署后访问 /import/seed-memories

importance 评分规则（1-10）：
- 9-10: 核心身份信息（名字、关系、最重要的承诺）
- 7-8:  重要偏好和习惯（饮食、作息、工作）
- 5-6:  有趣的事件和细节
- 3-4:  临时性信息
"""

from database import get_pool, save_memory, get_all_memories_count

SEED_MEMORIES = [
    # ======== 基础信息（改成你自己的） ========
    {"content": "用户的名字是小明", "importance": 9},
    {"content": "用户养了一只橘猫叫大橘", "importance": 7},
    {"content": "用户是程序员，主要写 Python", "importance": 7},
    {"content": "用户住在北京，喜欢吃火锅", "importance": 6},
    
    # ======== 偏好 ========
    {"content": "用户喜欢简洁的回答，不喜欢太啰嗦", "importance": 8},
    {"content": "用户是 INTJ，喜欢逻辑清晰的讨论", "importance": 6},
    
    # ======== 重要事件 ========
    {"content": "2026-01-01 用户和 AI 开始使用记忆系统", "importance": 7},
    
    # ======== 在这里继续添加更多记忆 ========
]


async def run_seed_import():
    """执行导入（自动跳过已存在的记忆）"""
    pool = await get_pool()
    before = await get_all_memories_count()
    
    imported = 0
    skipped = 0
    
    for mem in SEED_MEMORIES:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE content = $1",
                mem["content"],
            )
        
        if existing > 0:
            skipped += 1
            continue
        
        await save_memory(
            content=mem["content"],
            importance=mem["importance"],
            source_session="seed-import",
        )
        imported += 1
    
    after = await get_all_memories_count()
    
    return {
        "status": "done",
        "imported": imported,
        "skipped": skipped,
        "before": before,
        "after": after,
    }
