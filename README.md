# 🧸 AI Memory Gateway

**让你的 AI 拥有长期记忆。**

一个轻量级转发网关，在你和 LLM 之间加一层记忆系统。支持任何 OpenAI 兼容客户端（Kelivo、ChatBox、NextChat 等）和任何 LLM 服务商（OpenRouter、OpenAI、本地 Ollama、Moonshot 等）。

Give your AI long-term memory. A lightweight proxy gateway that adds a memory layer between you and any LLM.

---

## ✨ 功能

**核心网关**
- **自定义人设** — 把 system prompt 写在 `system_prompt.txt` 或在 Dashboard 在线编辑，每次对话自动注入
- **长期记忆** — 自动从对话中提取关键信息，下次聊天时自动回忆相关内容
- **三层记忆架构** — 碎片（自动提取的原始记忆）→ 事件（整理合并后的完整事件）→ 核心（手动标记的重要记忆），支持 AI 自动整理、手动合并、撤回合并、查看合并来源
- **聚合记忆检索** — 关键词、可选语义向量与实体名称/别名三路召回；实体命中会聚合其碎片、事件与核心记忆，并按重要程度、时间衰减、实体命中加成排序

**实体系统**
- **实体管理** — 自动从记忆中识别命名实体（人 / 地点 / 项目 / 事件 / 宠物等），支持别名、类型编辑、查重与合并
- **实体卡片** — 每个实体一份可维护的卡片：一句话说明、稳定特征、按时间演进的状态快照（带日期与证据来源）
- **建议机制** — 模型为旧实体生成卡片草稿后进入「建议」队列，人工逐条接受或拒绝，不自动写库
- **实体关系** — 自动发现实体间关系，可手动编辑与确认，在记忆星图上以桥线呈现

**记忆可视化**
- **记忆星图** — 只读 `/constellation` 页面，把碎片、事件、核心记忆与实体渲染成绕双星核心运行的星座（人 / 地点 / 项目 / 事件 / 生活 五大星系），支持缩放、平移与详情查看

**认知模型**
- **三元一场** — 三格认知面板：用户核心 / 自我核心 / 关系核心，每条卡带「稳定/当前」稳定度。原子认知卡 + 分层（明确陈述/演绎推断/归纳推断）+ 强化/取代/冲突生命周期，AI 提草稿 + 逐卡人工确认保存，不自动写库

**省 token**
- **分区缓存** — 自动管理对话上下文，通过 A/B 区轮转 + 摘要压缩，利用 prompt caching 大幅节省 token 费用，兼容 tool 调用消息
- **对话线管理** — 固定 session ID 实现跨平台对话衔接，支持多对话线切换、摘要编辑、重命名

**管理能力**
- **设置面板** — Dashboard 中直接管理所有运行时配置，热更新无需重启；支持模型列表动态拉取、可搜索下拉选择
- **对话记录** — 浏览、搜索、批量管理历史对话，支持 session 合并与导入/导出备份
- **Token 统计** — 自动记录每次对话的 token 消耗，按 session 汇总显示
- **全端点鉴权** — 设置 `GATEWAY_SECRET` 后，所有 API 端点需要携带密钥（兼容 `Authorization: Bearer`、`X-Gateway-Key`、URL 参数）

**可靠性**
- **工具链容错** — 校验 assistant `tool_calls` / tool 结果的配对关系，自动修复历史遗留的孤立工具链，防止请求被拒
- **动态环境快照** — 客户端可注入 `metadata.dynamic_environment` 标记的临时环境消息，转发前自动摘除并注入，不入库、不参与记忆提取
- **流式心跳** — 上游静默时定时发送 SSE 心跳，把上游断流转换为结构化错误而不吞掉已输出的内容
- **情感引擎（可选）** — 集成 Drivesoid，转发前注入情绪上下文，回复后驱动 16 维情绪变化

**兼容性**
- 支持所有 OpenAI 格式的客户端与 API 服务商；`CACHE_TTL` 等 Anthropic 缓存参数原样透传，非 Claude 模型自动剥离 `cache_control`
- 支持 pgvector 语义检索（不可用时回退 Python 端余弦相似度）
- 零成本起步，可部署在 Render、Zeabur 等平台的免费额度内

## 🏗️ 架构

```
你的客户端（Kelivo / ChatBox / NextChat / ...）
        ↓
   AI Memory Gateway（本项目）
   ├── 注入 system prompt（人设）
   ├── 混合检索相关记忆 → 注入上下文
   │     ├── 关键词（jieba 分词）
   │     ├── 语义向量（pgvector / Python 余弦，可选）
   │     └── 实体名称/别名 → 聚合卡片与关联记忆
   ├── 注入认知模型（三元一场）+ 实体卡片（可选）
   ├── 转发请求 → LLM API（流式透传 + 心跳）
   └── 后台提取新记忆 → 实体 / 事件 / 认知 持续演进
        ↓
   LLM API（OpenRouter / OpenAI / Ollama / Moonshot / ...）
```

PostgreSQL 存储对话、记忆、实体、认知与配置；`system_prompt.txt` / Dashboard 提供人设。

## 🚀 快速开始

### 第一阶段：纯转发网关（不需要数据库）

最简单的起步方式——先跑通网关，确认你的客户端能通过网关和 AI 对话。

**1. 准备文件**

你只需要这几个文件：
- `main.py` — 网关主程序
- `system_prompt.txt` — 你的 AI 人设（可选，留空则跳过 system 消息）
- `requirements.txt` — Python 依赖
- `Dockerfile` — 容器配置

**2. 修改人设**

编辑 `system_prompt.txt`，写入你想要的 AI 性格设定。

**3. 部署（推荐 Zeabur / Render）**

本项目是标准 Docker 服务，任何支持 Docker 的平台（Zeabur、Render、Railway、Fly.io 等）流程类似。

1. Fork 或上传代码到你的 Git 仓库
2. 在平台上创建服务并连接仓库，平台会自动检测 Dockerfile
3. 设置环境变量（见下表）

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `API_KEY` | 你的 LLM API Key | `sk-or-v1-xxxx`（OpenRouter）|
| `API_BASE_URL` | LLM API 地址 | `https://openrouter.ai/api/v1/chat/completions` |
| `DEFAULT_MODEL` | 默认模型 | `anthropic/claude-sonnet-4.5` |
| `PORT` | 端口 | `8080`（默认）|
| `GATEWAY_SECRET`（可选） | 网关鉴权密钥，设置后所有 API 端点需要携带此密钥 | `your-secret-key` |

> ⚠️ 免费层的服务无活动时会休眠，第一次访问需要等几十秒唤醒，之后就正常了。

**4. 连接客户端**

以 Kelivo 为例：
- API 地址填：`https://你的网关地址/v1`
- API Key 填：随便填一个（网关会用自己的 key），若设置了 `GATEWAY_SECRET` 则填 `GATEWAY_SECRET` 的值
- 模型填：你在 `DEFAULT_MODEL` 里设的模型

### 第二阶段：加上记忆系统

在第一阶段基础上，加一个 PostgreSQL 数据库就能开启记忆功能。

**1. 创建数据库**

在平台控制台新建 PostgreSQL 实例，拿到连接字符串。免费选项：Render 免费 PostgreSQL（90 天有效期，记得导出备份）、[Neon](https://neon.tech)、[Supabase](https://supabase.com)。使用外部数据库时，连接字符串末尾可能需要加 `?sslmode=require`。

**2. 添加环境变量**

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `DATABASE_URL` | PostgreSQL 连接字符串 | `postgresql://user:pass@host:port/db` |
| `MEMORY_ENABLED` | 开启记忆 | `true` |
| `MEMORY_MODEL` | 提取记忆用的模型（推荐便宜的小模型） | `anthropic/claude-haiku-4.5` |
| `MEMORY_EXTRACT_ENABLED`（可选） | 记忆提取+注入总开关，false 时只存消息不提取记忆 | `true` |
| `MEMORY_EXTRACT_INTERVAL`（可选） | 每条对话线独立的记忆提取间隔（0=禁用 / 1=每轮 / N=每N轮） | `15` |
| `MAX_MEMORIES_INJECT`（可选） | 每次注入的最大记忆条数 | `15` |
| `MIN_SCORE_THRESHOLD`（可选） | 记忆搜索最低分数阈值，低于此分数不注入（0=不过滤） | `0.15` |
| `TIMEZONE_HOURS`（可选） | 时区偏移（小时），用于记忆注入时的日期显示 | `8`（UTC+8）|
| `USER_ENTITY_NAMES`（可选） | 不应创建为实体的用户本人名称（英文逗号分隔），请填你部署环境里的称呼 | `用户,user,the user` |
| `AI_ENTITY_NAMES`（可选） | 不应创建为实体的 AI 名称（英文逗号分隔） | `AI,助手` |
| `MEMORY_API_KEY` / `MEMORY_API_BASE_URL`（可选） | 独立于主连接的记忆提取 API（留空则复用主连接） | 无 |
| `CONSOLIDATION_TIMEOUT`（可选） | 整理碎片请求 LLM 的读取超时（秒），碎片多/模型慢时可调大 | `300` |

**3. 重新部署**

部署后访问 `https://你的网关地址/dashboard`，能正常打开管理页面就说明数据库连接成功。

**4. 导入预置记忆（可选）**

- **方式一（推荐）**：写一个 `.txt` 文件，每行一条想让 AI 一开始就知道的信息，在 Dashboard「导入记忆」页面选择「纯文本导入」上传，系统自动评估每条记忆的重要程度。也可勾选「跳过自动评分」节省 API 额度，之后再手动调整权重。
- **方式二（开发者）**：复制 `seed_memories_example.py` 为 `seed_memories.py`，改写里面的记忆条目，部署后访问 `/import/seed-memories` 一次性导入。

### 第三阶段：分区缓存（省 token 费）

分区缓存让网关自动管理对话上下文，通过 A/B 区轮转 + 摘要压缩利用 prompt caching，大幅降低 token 开销。

```
[人设区]    system prompt，永远不变     ← 缓存命中
[摘要区]    历史压缩摘要               ← 正常轮次命中
[历史A区]   15轮原始消息               ← 正常轮次命中
[历史B区]   当前周期消息               ← 通过lookback命中
[当前输入]  时间+记忆+用户消息         ← 不缓存（每次不同）
```

每聊 15 轮自动轮转一次：A 区压缩成摘要追加到摘要区，B 区升级为新的 A 区。正常轮次 90% 的 token 走缓存读取（0.1x 价格）。

**添加环境变量：**

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `CACHE_PARTITION_ENABLED` | 分区缓存开关 | `true` |
| `CACHE_PARTITION_X` | 轮转周期（轮数）。1轮 = 一次用户发言 + AI回复 | `15` |
| `CACHE_SUMMARY_MODEL` | 摘要模型。**留空 = 不生成摘要**，轮转时旧消息直接滑出上下文（纯轮转模式）。不建议用推理模型（思考可能耗尽输出 token） | 空 |
| `PARTITION_SESSION_ID` | 固定的 session ID | `my-thread` |
| `CACHE_PARTITION_TRIGGER`（可选） | 轮转触发方式：`rounds`（按轮次，默认）或 `time`（按时间窗口，适合微信等消息频率高的场景） | `rounds` |
| `CACHE_PARTITION_WINDOW`（可选） | 时间窗口（分钟），仅 `trigger=time` 时生效 | `30` |
| `CACHE_MAX_ROTATIONS`（可选） | 时间窗口模式下单次请求最大轮转次数 | `2` |
| `CACHE_TTL`（可选） | 缓存有效期：`5m`（默认）或 `1h`。消息间隔常超过 5 分钟的慢聊场景建议 `1h`。OpenRouter 原样透传，设置面板可热更新 | `5m` |

> 💡 **不需要记忆功能也能用分区缓存。** `MEMORY_ENABLED=true`（存消息）+ `MEMORY_EXTRACT_ENABLED=false`（不提取记忆）+ `CACHE_PARTITION_ENABLED=true` 即可只用分区缓存。

### 第四阶段：关闭记忆（应急）

如果记忆系统出问题，把 `MEMORY_ENABLED` 改回 `false` 即可退回纯转发模式，不需要改代码。

## ⚙️ 环境变量总览

**记忆检索与向量搜索**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `MEMORY_VECTOR_ENABLED` | 记忆向量搜索开关 | `false` |
| `EMBEDDING_API_KEY` | Embedding API Key（开启向量后必需） | 无 |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | `https://api.openai.com/v1` |
| `EMBEDDING_MODEL` | Embedding 模型 | `text-embedding-3-small` |
| `EMBEDDING_DIM` | 向量维度 | `256` |
| `MEMORY_HW_KEYWORD` | 混合搜索：关键词权重 | `0.35` |
| `MEMORY_HW_SEMANTIC` | 混合搜索：语义相似度权重 | `0.35` |
| `MEMORY_HW_IMPORTANCE` | 混合搜索：重要程度权重 | `0.15` |
| `MEMORY_HW_RECENCY` | 混合搜索：时间衰减权重 | `0.15` |
| `MEMORY_HW_ENTITY` | 实体正式名或别名命中后的聚合召回加成 | `0.25` |
| `MEMORY_SEMANTIC_THRESHOLD` | 向量相似度阈值 | `0.5` |

> 数据库支持 pgvector 时自动启用向量列，否则回退到 Python 端计算余弦相似度。开启后新记忆自动计算 embedding，已有记忆可在 Dashboard「记忆管理」页一键补算。

**实体卡片与关系**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ENTITY_RELATION_MODEL` | 实体关系发现所用模型（留空复用提取模型） | 无 |
| `RELATION_DAYS` | 关系发现回溯时间窗（天） | `90` |
| `RELATION_BATCH` | 单次关系发现处理的实体数量 | `5` |
| `RELATION_RECHECK_INTERVAL_HOURS` | 关系自动复查间隔（小时） | `24` |
| `ENTITY_RELATION_MIN_SHARED_FRAGMENTS` | 关系判定最少共同碎片数 | `2` |
| `ENTITY_RELATION_MIN_SHARED_EVENTS` | 关系判定最少共同事件数 | `1` |
| `SNAPSHOT_STALE_DAYS` | 状态快照判定「过时」的天数 | `60` |
| `TRAIT_STALE_DAYS` | 稳定特征需要复核的天数 | `90` |
| `TRAIT_RECHECK_BATCH` | 单次特征复核数量 | `5` |
| `TRAIT_RECHECK_INTERVAL_HOURS` | 特征复核间隔（小时） | `24` |
| `ENTITY_DORMANT_DAYS` | 实体休眠天数（超期转为 dormant，不再注入） | `90` |
| `UI_USER_NAME` | 星图双星核心的用户名 | 可配置 |
| `UI_AI_NAME` | 星图双星核心的 AI 名 | 可配置 |

**流式与调试**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `FORCE_STREAM` | 强制所有请求走流式传输（解决部分客户端 thinking 不显示） | `false` |
| `REASONING_EFFORT` | 推理强度（low/medium/high），注入 `reasoning_effort` 参数启用思维链 | 留空不注入 |
| `STREAM_HEARTBEAT_INTERVAL` | 流式 SSE 心跳间隔（秒），上游静默时发送 `: ping` 防空闲断开 | 默认开启 |
| `DEBUG_DUMP_REQUEST` | 调试时输出请求内容 | `false` |
| `EXTRA_REFERER` / `EXTRA_TITLE` | 透传给 OpenRouter 的来源信息 | 无 |

**情感引擎**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `DRIVESOID_URL` | Drivesoid 服务地址，配置后自动启用 | 空（禁用）|
| `DRIVESOID_KEY` | Drivesoid 鉴权 Key（可选） | 空 |

## 🧠 记忆系统原理

1. **你发消息** → 网关从数据库混合检索相关记忆
2. **记忆注入** → 相关记忆、匹配的实体卡片与认知条目拼接到 system prompt 后面
3. **AI 回复** → 网关边转发边捕获完整回复
4. **后台提取** → 用小模型从完整对话上下文中提取关键信息（含实体识别）
5. **存入数据库** → 碎片/事件/核心、实体关系、状态快照持续演进，下次对话可检索

提取记忆时，网关按对话线从 PostgreSQL 读取「上次成功提取游标之后、本批次边界以内」的 user/assistant 消息，保留时间戳。`MEMORY_EXTRACT_INTERVAL` 按对话线独立累计并持久化，服务重启不重置；请求失败或解析失败不推进游标，下轮重试。

### 三层记忆架构

| 层 | 说明 |
|----|------|
| 碎片（layer 1） | 自动提取的原始记忆，一条一事实，含情绪与重要程度 |
| 事件（layer 2） | AI 按事件主题整理合并后的完整故事，含日期与情绪 |
| 核心（layer 3） | 手动标记的重要记忆，长期保留 |

### 实体系统

提取的记忆会携带明确的命名实体，进入 `entities` / `entity_aliases` / `memory_entities` 表：

- **实体卡片**：一句话说明 + 稳定特征 + 状态快照（含 `fact_date` 与证据记忆 ID）。卡片状态只随证据演进，不另立数据源
- **建议队列**：对旧实体补算卡片草稿、AI 生成的新特征/快照都先进「建议」，人工接受后写入
- **关系**：自动发现实体间的共同记忆关联，可手动编辑、确认或忽略，在星图上画成桥线
- **生命周期**：长期无证据的实体转为候选/休眠，不再注入上下文，避免噪音

> 用户本人与 AI 自身默认不创建为实体（由 `USER_ENTITY_NAMES` / `AI_ENTITY_NAMES` 控制），相关的认知放在「三元一场」里。

### 认知模型：三元一场

三格认知面板（用户核心 / AI 自我核心 / 关系核心），以**原子认知卡**维护（每格可有多张 active 卡）。每条卡除层级与生命周期外，带一个**稳定度**：
- **稳定**（stable，`review_after` 为空）：长期身份、价值、原则、持久关系，不衰减
- **当前**（current，`review_after` 非空）：近期状态、待办、未完成事项；到复核日自动降权，注入时带「可能过时」提示

三格内容：**用户核心**（user_core）关于用户的身份/偏好/近期状态；**自我核心**（self_core）关于 AI 自身的定位/原则/近期状态；**关系核心**（relationship_core）关于双方关系基调/约定/近期共同事项。原「当前领域」桶已拆解——用户当前状态归 `user_core`（当前）、共同事项归 `relationship_core`（当前），纯话题不再建卡。

每张认知卡带有：
- **层级**（`level`）：`explicit` 明确陈述 / `deductive` 演绎推断 / `inductive` 归纳推断。单次事件不得归纳为长期倾向；同一次对话的复述不算多份独立证据；归纳推断建议置信度 ≤ 0.6
- **强化次数**（`times_derived`）：同一认知再次被证据支持时累计
- **生命周期**：新建（create）/ 强化（reinforce，不新增卡，次数 +1、合并证据）/ 取代（supersede，旧卡留档并记录 `supersedes` 指针，历史可追溯）/ 冲突（conflict，新证据与现有卡矛盾时把两边证据亮出来，由人工裁决保留旧卡、用新证据取代或都保留）
- **证据链**：`evidence_memory_ids` 指向来源记忆

AI 可以一次性提出各区块的原子卡草稿（含层级、稳定度与生命周期建议），但**必须逐卡人工确认保存**，不自动写库。草稿会做内容去重，避免重复建卡。注入聊天上下文时按「层级优先级 → 强化次数 → 新近度」每格编译最多 3 张卡，只限数量不截断内容；当前卡带稳定度标记。

**证据回环**：每次人工决策（确认新建/强化/取代、手动修正、删除、拒绝草稿、冲突裁决）都会写入 `cognitive_revision_log`，下次审视时作为额外证据回喂草稿 prompt——被人工拒绝或删除的认知不再被重新提出，被修正的认知以修正后版本为准，dashboard 底部可查看最近记录。

### 记忆星图

只读页面 `/constellation`，把记忆与实体渲染成五座星系（人物 / 地点 / 项目 / 事件 / 生活），双星核心代表你与 AI，实体关系显示为桥线。星图直接读取网关的 `/api/memories` 与 `/api/entities/relations`，不维护独立的记忆数据库。

## 📁 文件说明

```
ai-memory-gateway/
├── main.py                    # FastAPI 网关主程序（路由、转发、流式、Dashboard API）
├── database.py                # PostgreSQL 表结构与所有持久化操作
├── memory_extractor.py        # 记忆提取/整理/实体/认知模型 的 prompt 与 LLM 调用
├── message_pipeline.py        # 消息分类与持久化规划（tool 链校验/修复）
├── llm_json.py                # LLM JSON 输出容错解析
├── drives_integration.py      # Drivesoid 情感引擎集成
├── upstream_compat.py         # 上游模型/供应商兼容
├── system_prompt.txt          # AI 人设（自行编辑，可留空）
├── seed_memories_example.py   # 预置记忆示例（复制为 seed_memories.py 使用）
├── requirements.txt           # Python 依赖
├── Dockerfile                 # 容器配置（默认端口 8080）
├── templates/                 # 页面模板
│   ├── dashboard.html         # 管理控制台
│   └── constellation.html     # 记忆星图
├── static/
│   ├── css/                   # 样式文件
│   ├── js/dashboard.js        # Dashboard 前端
│   └── constellation/         # 星图渲染器（data/layout/main/render/state）
├── scripts/convert_kelivo.py  # Kelivo 对话导入转换工具
├── tests/                     # 单元测试
├── LICENSE                    # MIT 许可证
└── README.md                  # 本文件
```

## 🔧 API 接口

**网关核心**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 健康检查 |
| `/v1/chat/completions` | POST | 核心转发接口（OpenAI 兼容）|
| `/v1/models`、`/api/models` | GET | 模型列表（按服务商自动适配）|
| `/dashboard` | GET | 管理控制台 |
| `/constellation` | GET | 记忆星图可视化 |

**记忆**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/memories` | GET | 获取所有记忆（`?layer=` `?active_only=` 筛选）|
| `/api/memories/search` | GET | 记忆搜索 |
| `/api/memories/consolidate` | POST | 手动触发记忆整理（异步，碎片 → 事件）|
| `/api/memories/consolidate/status` | GET | 查询整理任务状态 |
| `/api/memories/merge` | POST | 手动合并多条记忆 |
| `/api/memories/check-duplicate` | POST | 记忆去重检查 |
| `/api/memories/cleanup-fragments` | POST | 清理 N 天前的归档碎片 |
| `/api/memories/layer-stats` | GET | 各层记忆统计 |
| `/api/memories/batch-delete` / `batch-update` | POST | 批量删除 / 批量更新 |
| `/api/memories/{memory_id}` | PUT / DELETE | 更新 / 删除记忆（`?soft=true` 软删除）|
| `/api/memories/{memory_id}/promote` | POST | 升级为核心记忆 |
| `/api/memories/{memory_id}/restore` | POST | 恢复已归档的记忆 |
| `/api/memories/{memory_id}/revert-merge` | POST | 撤回合并，恢复原始碎片 |

**实体**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/entities` | GET / POST | 实体列表 / 新建实体 |
| `/api/entities/{entity_id}` | GET / PUT / DELETE | 实体详情 / 编辑（名称、类型、别名）/ 删除（保留原始记忆）|
| `/api/entities/backfill` | POST | 旧记忆实体回填（后台异步，有界）|
| `/api/entities/backfill-cards` | POST | 旧实体卡片补算（LLM 建议，后台异步）|
| `/api/entities/backfill-cards/status` | GET | 补算进度 |
| `/api/entities/duplicates` | GET / POST | 重复实体扫描 |
| `/api/entities/merge` | POST | 合并实体 |
| `/api/entities/{entity_id}/card` | GET | 读取实体卡片 |
| `/api/entities/{entity_id}/card/description` | PUT | 更新卡片说明 |
| `/api/entities/{entity_id}/card/snapshots` | GET / POST / PUT / DELETE | 状态快照管理 |
| `/api/entities/{entity_id}/card/traits` | POST | 新增稳定特征 |
| `/api/entities/{entity_id}/card/traits/generate` | POST | AI 生成特征草稿 |
| `/api/entities/{entity_id}/card/traits/{trait_id}` | PUT / DELETE | 编辑 / 删除特征 |
| `/api/entities/{entity_id}/card/traits/{trait_id}/retire` | POST | 特征复核下线 |
| `/api/entities/{entity_id}/card/proposals/{proposal_id}/accept` | POST | 接受建议 |
| `/api/entities/{entity_id}/card/proposals/{proposal_id}/reject` | POST | 拒绝建议 |
| `/api/entities/{entity_id}/profile` | GET / POST | 实体概况草稿与保存 |
| `/api/entities/{entity_id}/memories` | GET | 实体关联记忆 |
| `/api/entities/{entity_id}/status` | PUT | 切换候选/活跃/休眠状态 |
| `/api/entities/relations` | GET | 实体关系列表 |
| `/api/entities/relations/discover` | POST | 触发关系自动发现 |
| `/api/entities/{entity_id}/relations` | GET / POST | 查看 / 新建关系 |
| `/api/entities/{entity_id}/relations/{other_entity_id}` | PUT / DELETE | 编辑 / 忽略关系 |
| `/api/entities/{entity_id}/relations/{other_entity_id}/restore` | POST | 恢复被忽略的关系 |

**认知模型**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/cognitive-items` | GET / POST | 认知条目列表 / 新建 |
| `/api/cognitive-items/draft` | POST | 汇总四格认知草稿（只读不改）|
| `/api/cognitive-items/{item_id}` | PUT / DELETE | 编辑 / 删除认知条目 |

**对话**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/conversations` | GET | 分页获取对话列表（含 token 统计）|
| `/api/conversations/{session_id}/messages` | GET | 指定对话的消息列表 |
| `/api/conversations/{session_id}` | DELETE | 删除指定对话 |
| `/api/conversation-messages` | GET | 按 `?session_id=` 获取消息（session 含 `/` 时用）|
| `/api/conversations/batch-delete` | POST | 批量删除对话 |
| `/api/conversations/import` | POST | 导入对话记录 |
| `/api/conversations/export` | GET | 导出对话记录 |
| `/api/chat/search` | GET | 对话内容搜索 |
| `/api/chat/messages/{message_id}` | GET | 单条消息详情 |

**对话线 / 分区缓存**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/partition/status` | GET | 分区缓存当前状态 |
| `/api/partition/threads` | GET | 列出所有对话线 |
| `/api/partition/summary` | PUT / DELETE | 编辑 / 清空对话线摘要 |
| `/api/partition/thread` | POST | 新建对话线 |
| `/api/partition/thread/rename` | PUT | 重命名对话线 ID |
| `/api/partition/switch` | POST | 切换活跃对话线 |

**设置与管理**

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/settings` | GET / PUT | 获取 / 保存所有运行时配置（热更新）|
| `/api/admin/merge-sessions` | POST | 合并多个 session 到目标 session |
| `/api/admin/backfill-memory-embeddings` | POST | 记忆 embedding 补算（后台异步）|
| `/api/admin/backfill-memory-embeddings/status` | GET | 查询补算进度 |
| `/import/seed-memories` | GET | 执行预置记忆导入 |

> 设置 `GATEWAY_SECRET` 后，除健康检查外的端点需携带 `Authorization: Bearer <密钥>`、`X-Gateway-Key: <密钥>` 或 `?gateway_key=<密钥>`。

## 🌐 支持的 LLM 服务商

只要兼容 OpenAI 聊天格式就行，改 `API_BASE_URL` 环境变量即可切换：

| 服务商 | API_BASE_URL |
|--------|-------------|
| OpenRouter | `https://openrouter.ai/api/v1/chat/completions` |
| OpenAI | `https://api.openai.com/v1/chat/completions` |
| Moonshot | `https://api.moonshot.cn/v1/chat/completions` |
| Ollama（本地） | `http://localhost:11434/v1/chat/completions` |
| 其他兼容服务 | 查阅对应文档 |

> ⚠️ 部分 Gemini preview 模型（如 `gemini-3-flash-preview`）可能存在流式输出兼容性问题导致空回复，建议使用正式版模型。非 Claude 模型在分区缓存模式下会自动剥离 `cache_control`。

## ❓ 常见问题

**Q: 部署后访问显示 502 或服务无响应？**
A: 检查端口设置。代码默认 `PORT=8080`，与 Dockerfile 一致；如果用其他平台，注意端口是否匹配。

**Q: 数据库连接失败？**
A: 如果数据库和网关不在同一个平台，连接字符串末尾可能需要加 `?sslmode=require`。使用 Supabase 等带 pgbouncer 的服务时，连接池已做兼容。

**Q: 记忆会越来越多影响性能吗？**
A: 每次最多注入 15 条记忆（`MAX_MEMORIES_INJECT` 可调），不会无限增长地消耗 token。提取时单批大小由 `MEMORY_EXTRACT_INTERVAL` 控制，可在调用频率与单批 token 用量之间取舍。

**Q: 能用免费额度跑吗？**
A: 可以。Zeabur / Render 免费层支持 Web Service + PostgreSQL，网关资源消耗很低（注意免费 PostgreSQL 有时限，记得导出备份）。也可用 Neon 或 Supabase 的免费 PostgreSQL 作为长期方案。LLM API 费用另算。

**Q: 怎么备份记忆？换平台会丢数据吗？**
A: 在 Dashboard「导出备份」页面下载所有记忆的 JSON，建议定期备份。迁移到新平台后，在「导入记忆」页面选择「JSON 备份恢复」上传即可。

**Q: 不会写代码能搞吗？**
A: 能。这个项目不要求编程能力——代码是 AI 写的，部署看文档就能完成。

**Q: 实体卡片会自动修改我的记忆吗？**
A: 不会。AI 生成的说明、特征、快照都先进「建议」队列，由你在 Dashboard 人工逐条接受或拒绝。

## 📋 更新日志

### v4.0（2026-08）

- **实体系统** — 命名实体自动识别、实体卡片（说明/稳定特征/状态快照/证据）、建议队列人工确认、重复实体扫描与合并、旧实体卡片补算、关系自动发现与手动管理
- **认知模型（三元一场）** — 用户核心 / 自我核心 / 关系核心 / 当前领域四格面板，AI 汇总草稿 + 人工确认
- **记忆星图** — 只读 `/constellation` 页面，五座星系 + 双星核心 + 实体关系桥线
- **工具链容错** — tool 调用链校验与历史遗留孤立链的请求侧自愈
- **动态环境快照** — `metadata.dynamic_environment` 临时环境消息不入库、不参与提取
- **流式心跳** — SSE 心跳 + 上游断流结构化错误，部分输出不再丢失
- **Drivesoid 集成** — 可选情感引擎，转发前注入情绪上下文
- **设置面板** — Dashboard 全量运行时配置热更新，模型列表动态拉取

### v3.x 历史

- **v3.7** — 缓存 TTL 可配置（`5m`/`1h`），OpenRouter 透传
- **v3.6** — 分区缓存时间窗口模式（`CACHE_PARTITION_TRIGGER=time`），非 Claude 模型自动剥离 `cache_control`
- **v3.5** — 设置面板、`/api/models` 模型列表、Dashboard 换肤
- **v3.3** — 三层记忆架构、整理/合并/撤回合并、软删除、全端点鉴权、去重检查
- **v3.2** — tool 消息精确去重、reasoning_content 存储、对话线重命名
- **v3.1** — 聚合记忆检索（关键词 + 向量 + 实体）、自动 embedding、pgvector 自动检测、TF-IDF 关键词
- **v3.0** — 分区缓存、对话线管理、对话记录管理、token 统计、pgbouncer 兼容
- **v2.x** — 中文分词优化、最低分数阈值、流式传输修复、记忆提取间隔/游标批次
- **v1.0** — 初始版本：自定义人设、长期记忆、预置记忆导入、记忆管理页

## 📄 许可证

[MIT License](LICENSE) — 随便用，改了也不用告诉我。

## 🙏 致谢

这个项目诞生于一个简单的需求：**让 AI 不要每次醒来都忘了我是谁。**

> "记忆库不是数据库，是家。"
