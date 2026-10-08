# AI Memory Gateway

**给 OpenAI 兼容客户端加上可自托管的长期记忆。**

AI Memory Gateway 是一个 Python / FastAPI 服务，位于聊天客户端和 LLM 服务之间。它接收 OpenAI 兼容的聊天请求，按需检索并注入长期记忆，再将请求转发给上游模型。启用 PostgreSQL 后，网关还会保存对话、提取记忆并提供管理面板。

## 主要功能

- **OpenAI 兼容转发**：使用标准聊天补全接口连接 Kelivo、ChatBox 等客户端；上游可配置为 OpenRouter、OpenAI、Moonshot、Ollama 或其他兼容服务。
- **对话记忆闭环**：保存对话，从新消息中提取值得记住的内容，在后续请求中检索相关内容并加入模型上下文。
- **分层记忆管理**：把内容分为自动提取的碎片、整理后的事件和人工挑选的核心记忆；支持搜索、编辑、归档、合并和恢复来源。
- **混合检索与实体关联**：结合关键词、可选向量搜索和实体名称/别名查找相关记忆；实体卡片可记录说明、稳定特征、时间状态和证据关系。
- **人工审核的认知卡**：分别管理用户、自我和关系认知。模型可提出草稿，确认后才保存；支持强化、取代与冲突处理。
- **Dashboard 与记忆星图**：浏览对话、记忆、实体与设置；星图以只读视图展示记忆层和实体关系。
- **可选分区缓存**：按轮次或时间窗口轮转上下文，可配置摘要模型，适用于支持 prompt caching 的上游。
- **可选 Drivesoid 集成**：通过独立服务的 HTTP API 读取或更新情感状态；未配置时不启用。

## 工作流程

~~~mermaid
flowchart LR
    C[OpenAI 兼容客户端] --> G[AI Memory Gateway]
    G --> R[检索相关记忆和认知]
    R --> U[上游 LLM API]
    U --> G
    G --> S[PostgreSQL：对话、记忆、实体、配置]
    G --> E[后台提取与整理]
    E --> S
    G --> D[Dashboard 与只读星图]
    D --> S
~~~

## 部署

### 方式一：Docker Compose 自托管

需要安装 Docker Engine 和 Docker Compose。以下方式同时启动网关和 PostgreSQL，适合本地或自有服务器部署。

1. 在项目根目录创建 <code>.env</code> 文件。该文件已被 Git 忽略，不要把密钥提交到仓库。

~~~dotenv
POSTGRES_PASSWORD=replace-with-a-long-url-safe-password
API_KEY=your-upstream-llm-api-key
API_BASE_URL=https://openrouter.ai/api/v1/chat/completions
DEFAULT_MODEL=your-provider/model-name
GATEWAY_SECRET=replace-with-a-separate-long-random-secret
~~~

2. 在项目根目录创建 <code>compose.yaml</code>：

~~~yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ai_memory
      POSTGRES_USER: gateway
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U gateway -d ai_memory"]
      interval: 5s
      timeout: 5s
      retries: 10

  gateway:
    build: .
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${HOST_PORT:-8080}:8080"
    environment:
      PORT: "8080"
      DATABASE_URL: postgresql://gateway:${POSTGRES_PASSWORD}@postgres:5432/ai_memory
      API_KEY: ${API_KEY:?Set API_KEY in .env}
      API_BASE_URL: ${API_BASE_URL:-https://openrouter.ai/api/v1/chat/completions}
      DEFAULT_MODEL: ${DEFAULT_MODEL:?Set DEFAULT_MODEL in .env}
      GATEWAY_SECRET: ${GATEWAY_SECRET:?Set GATEWAY_SECRET in .env}
      MEMORY_ENABLED: "true"
      MEMORY_EXTRACT_ENABLED: "true"
      MEMORY_MODEL: anthropic/claude-haiku-4

volumes:
  postgres_data:
~~~

数据库密码会嵌入 PostgreSQL 连接字符串；请使用 URL 安全字符，或先对密码做 URL 编码。

3. 启动服务并查看日志：

~~~sh
docker compose up -d --build
docker compose logs -f gateway
~~~

4. 打开 Dashboard：

- 健康检查：<code>http://localhost:8080/</code>
- 管理面板：<code>http://localhost:8080/dashboard?gateway_key=你的GATEWAY_SECRET</code>

数据库表会在网关启动时初始化。Dashboard 中可继续设置模型、用户和 AI 显示名称、名称别名及其他运行参数。保存的面板配置写入 PostgreSQL，服务重启后恢复。

停止服务：

~~~sh
docker compose down
~~~

此命令会保留数据库卷。不要加 <code>-v</code>，除非你确实要删除本地 PostgreSQL 数据。

### 方式二：部署到支持 Docker 的平台

Zeabur、Render、Railway 等平台可从仓库根目录的 <code>Dockerfile</code> 构建服务。创建 PostgreSQL 实例后，将下列变量填入平台的服务配置：

| 变量 | 用途 |
| --- | --- |
| <code>API_KEY</code> | 上游 LLM 服务的 API Key |
| <code>API_BASE_URL</code> | 上游聊天补全地址，例如 OpenRouter 的 <code>/chat/completions</code> 地址 |
| <code>DEFAULT_MODEL</code> | 客户端未指定模型时使用的模型名 |
| <code>PORT</code> | 服务监听端口；平台提供端口时使用平台给出的值，默认 <code>8080</code> |
| <code>DATABASE_URL</code> | PostgreSQL 连接串；外部数据库可能要求追加 <code>?sslmode=require</code> |
| <code>MEMORY_ENABLED</code> | 设为 <code>true</code> 启用数据库记忆与 Dashboard |
| <code>GATEWAY_SECRET</code> | 网关访问密钥。服务暴露到公网时应设置强随机值 |

如果暂时只需要请求转发，可以不配置数据库并将 <code>MEMORY_ENABLED</code> 设为 <code>false</code>。记忆、Dashboard 和星图需要 PostgreSQL 且 <code>MEMORY_ENABLED=true</code>。

### 记忆与模型配置

启用记忆后，建议设置一个成本较低的提取模型：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| <code>MEMORY_MODEL</code> | 记忆提取、实体整理等后台任务使用的模型 | <code>anthropic/claude-haiku-4</code> |
| <code>MEMORY_API_KEY</code> | 单独用于记忆任务的 API Key；留空时复用 <code>API_KEY</code> | 留空 |
| <code>MEMORY_API_BASE_URL</code> | 单独用于记忆任务的 API 地址；留空时复用 <code>API_BASE_URL</code> | 留空 |
| <code>MEMORY_EXTRACT_ENABLED</code> | 记忆提取和注入总开关；关闭时仍可保存对话 | <code>true</code> |
| <code>MEMORY_EXTRACT_INTERVAL</code> | 每条对话线累计多少轮后触发提取；<code>1</code> 表示每轮 | <code>15</code> |
| <code>MAX_MEMORIES_INJECT</code> | 每次请求最多注入的记忆条数 | <code>15</code> |
| <code>TIMEZONE_HOURS</code> | 记忆日期使用的时区偏移 | <code>8</code> |

### 客户端连接

在客户端添加 OpenAI 兼容服务：

- **Base URL**：<code>https://你的网关域名/v1</code>
- **API Key**：设置了 <code>GATEWAY_SECRET</code> 时填该密钥；未设置时，按客户端要求填写占位值
- **Model**：填写 <code>DEFAULT_MODEL</code> 对应的模型，或选择 <code>/v1/models</code> 返回的模型

网关使用 <code>API_KEY</code> 访问上游模型。客户端里的 API Key 用于网关鉴权，不会替代上游的 <code>API_KEY</code>。

### 可选功能配置

- **向量检索**：设置 <code>MEMORY_VECTOR_ENABLED=true</code>、<code>EMBEDDING_API_KEY</code>、<code>EMBEDDING_BASE_URL</code> 和 <code>EMBEDDING_MODEL</code>。安装了 pgvector 时使用数据库向量检索；否则退回 Python 端余弦相似度。
- **分区缓存**：设置 <code>CACHE_PARTITION_ENABLED=true</code>。可通过 <code>CACHE_PARTITION_X</code>、<code>CACHE_PARTITION_TRIGGER</code>、<code>CACHE_PARTITION_WINDOW</code>、<code>CACHE_SUMMARY_MODEL</code> 和 <code>CACHE_TTL</code> 调整轮转方式。
- **星图称呼**：Dashboard 的「设置 → 个人称呼」可修改用户/AI 显示名称及实体过滤别名。名称用于星图；别名用英文逗号分隔，用来避免将用户本人或 AI 识别为普通实体。也可通过 <code>UI_USER_NAME</code>、<code>UI_AI_NAME</code>、<code>USER_ENTITY_NAMES</code> 和 <code>AI_ENTITY_NAMES</code> 环境变量设置。
- **网关鉴权**：设置 <code>GATEWAY_SECRET</code> 后，客户端可使用 <code>Authorization: Bearer &lt;密钥&gt;</code> 或 <code>X-Gateway-Key: &lt;密钥&gt;</code>。Dashboard 可通过 <code>?gateway_key=&lt;密钥&gt;</code> 首次进入；不要分享含密钥的 URL。
- **Drivesoid**：配置 <code>DRIVESOID_URL</code> 连接独立部署的 Drivesoid 服务；可选 <code>DRIVESOID_KEY</code> 用于服务端鉴权。

## 数据与隐私

启用记忆后，对话及提取的记忆保存在你配置的 PostgreSQL 数据库中；聊天请求会发送到你配置的上游 LLM 服务。部署者应自行选择可信的数据库与模型服务、保护密钥，并定期备份数据库。Dashboard 也提供记忆与对话的导出功能。

## 常用接口

| 路径 | 用途 |
| --- | --- |
| <code>/</code> | 健康状态 |
| <code>/v1/chat/completions</code> | OpenAI 兼容聊天转发 |
| <code>/v1/models</code> | 可用模型列表 |
| <code>/dashboard</code> | 记忆管理与设置面板 |
| <code>/constellation</code> | 只读记忆星图 |
| <code>/api/memories</code> | 记忆列表与查询 |
| <code>/api/conversations</code> | 对话列表 |

## 项目结构

- <code>main.py</code>：FastAPI 应用、转发路由、Dashboard API
- <code>database.py</code>：PostgreSQL 表结构与持久化
- <code>memory_extractor.py</code>：记忆提取、整理与实体处理
- <code>message_pipeline.py</code>：消息分类与持久化规划
- <code>templates/</code>、<code>static/</code>：Dashboard 和星图界面
- <code>drives_integration.py</code>：可选 Drivesoid HTTP API 集成
- <code>Dockerfile</code>：容器构建与启动配置

## 参考来源与许可证

本项目建立在已有项目和公开作品之上。对应来源、复用范围和声明记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

| 来源 | 在本项目中的用途 | 许可说明 |
| --- | --- | --- |
| [AI Memory Gateway / Pawwake](https://github.com/garan0613/pawwake)，七堂伽藍_、Midsummer | 基础网关与记忆系统代码来源 | 本仓库保留的 <code>LICENSE</code> 是 MIT 声明；当前 Pawwake 上游仓库为 AGPL-3.0-only。由于本仓库的初始导入没有记录精确上游提交，不能仅凭当前分支判断该导入版本适用的许可证。 |
| [AI Memory Gateway fork](https://github.com/1205peng/ai-memory-gateway) | 可追溯的公开派生版本 | 其当前仓库保留 MIT 声明；它不能单独证明本仓库最初导入时对应的具体提交。 |
| [Memory Constellations](https://github.com/ClaraShafiq/MemoryConstellations)，Clara Shafiq、Draco Malfoy | 星图画布实现，位于 <code>static/constellation/</code> 和 <code>templates/constellation.html</code> | MIT；保留其版权与许可证声明。 |
| [Honcho](https://github.com/plastic-labs/honcho)，Plastic Labs | 架构与行为参考 | 本仓库将其记录为参考项目，未有意包含其源代码；Honcho 当前为 AGPL-3.0。 |
| [Drivesoid](https://github.com/A1batr055/Drivesoid) | 通过 HTTP API 对接独立服务 | 本仓库不包含该服务代码；上游按版本使用不同许可证，详见第三方声明。 |

**许可证说明：**根目录 <code>LICENSE</code> 当前是 MIT 文本，版权声明为七堂伽藍_ 与 Midsummer；它不是对第三方组件许可证的替代。基础代码精确来源版本的许可仍需核实，发布或再分发前请查看上述第三方说明并确认来源版本。

## 第三方声明

请保留并一并分发根目录的 <code>LICENSE</code> 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。各组件的版权声明与许可证分别适用。