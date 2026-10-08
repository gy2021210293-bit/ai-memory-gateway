# 在 Zeabur 部署 AI Memory Gateway

这份说明补充 [README 的部署步骤](../README.md#推荐zeabur-部署)。项目作者个人使用 Zeabur 部署；下面描述的是从 GitHub 仓库部署网关，并在同一个 Zeabur 项目内运行 PostgreSQL 的方式。

## 1. 创建两个服务

1. 在 Zeabur 创建项目，添加 **Databases → PostgreSQL**。记忆和对话由 PostgreSQL 持久保存，部署网关时不需要另建数据库表。
2. 添加 **GitHub** 服务，授权访问本仓库，选择 `main` 分支。仓库根目录有 `Dockerfile`，Zeabur 会用它构建网关；此路径不会使用 README 中供本地自托管的 `compose.yaml`。
3. 若你把本项目放进更大的仓库，请让网关服务的构建目录指向包含 `Dockerfile`、`main.py` 和 `requirements.txt` 的目录。本仓库独立部署时无需调整目录。

## 2. 配置网关变量

在**网关服务**的 Variables 页面添加：

| 变量 | 示例或要求 |
| --- | --- |
| `DATABASE_URL` | `${POSTGRES_CONNECTION_STRING}`，引用同项目 PostgreSQL 服务暴露的内部连接串 |
| `MEMORY_ENABLED` | `true`，启用对话保存、记忆和 Dashboard |
| `API_KEY` | 上游 LLM 服务提供的密钥 |
| `API_BASE_URL` | 上游 OpenAI 兼容的**完整**聊天补全地址，须以 `/chat/completions` 结尾 |
| `DEFAULT_MODEL` | 上游服务支持的默认模型名 |
| `MEMORY_MODEL` | 上游服务支持的记忆提取模型名 |
| `GATEWAY_SECRET` | 自行生成的强随机值，用于保护公开网关及 Dashboard |

`MEMORY_API_KEY` 和 `MEMORY_API_BASE_URL` 可选。设置后后台记忆任务走独立的上游服务；留空则复用 `API_KEY` 和 `API_BASE_URL`。`MEMORY_MODEL` 必须是对应上游实际支持的模型。`MEMORY_EXTRACT_ENABLED` 默认是 `true`，如只想保存对话而暂时关闭记忆提取和注入，可设置为 `false`。

Zeabur 会向服务注入 `PORT`。程序读取 `PORT`，未提供时才使用 `8080`。不要为 Zeabur 手动复制本地 Compose 的 `PORT: 8080`。Zeabur 的 Git 服务在 Dockerfile 未声明端口时默认使用 `8080`。

**连接串注意：**网关读取的是 `DATABASE_URL`，不是 `POSTGRES_CONNECTION_STRING`。后者是 Zeabur PostgreSQL 服务暴露的变量，所以要在网关服务中做上述映射。若一个项目内有多个 PostgreSQL 服务，先在变量预览中确认引用来自目标数据库。数据库仍连不上时，在 PostgreSQL 服务的 Networking 页面核对内部主机与端口。不要把数据库公网连接串或密码写入 Git 仓库。

## 3. 绑定域名并连接客户端

在网关服务的 Domains 页面生成 `zeabur.app` 域名，或按 Zeabur 提示绑定自有域名。打开 `https://你的域名/`，确认网关返回健康状态，然后打开 `https://你的域名/dashboard?gateway_key=你的GATEWAY_SECRET`。包含密钥的 URL 适合首次进入面板，不要转发、截图公开或写入 README。

在 OpenAI 兼容客户端中设置：

- Base URL：`https://你的域名/v1`
- API Key：网关的 `GATEWAY_SECRET`，不是上游 `API_KEY`
- Model：`DEFAULT_MODEL` 对应的模型，或网关 `/v1/models` 返回的模型

客户端连接后发一条简单消息，确认请求能返回；接着在 Dashboard 查看对话是否保存。记忆提取按配置的轮次在后台运行，不一定在第一条消息后立即生成。

## 常见问题

| 现象 | 先检查 |
| --- | --- |
| 构建未使用 Dockerfile | GitHub 服务选择的分支、构建目录，以及目录中是否有 `Dockerfile` |
| 域名打不开 | 网关服务构建/运行日志、Domains 绑定状态，以及运行日志中的监听端口 |
| `DATABASE_URL 未设置` 或数据库连接失败 | 是否把 `${POSTGRES_CONNECTION_STRING}` 填在**网关服务**的 `DATABASE_URL`，以及 PostgreSQL 是否位于同一项目并可用 |
| `/dashboard` 提示记忆未启用 | `MEMORY_ENABLED=true`、`DATABASE_URL` 和网关重启后的运行状态 |
| 客户端收到 401 | 客户端 API Key 是否填写 `GATEWAY_SECRET`；不要填上游 `API_KEY` |
| 聊天可用但没有新记忆 | `MEMORY_EXTRACT_ENABLED`、`MEMORY_MODEL` 是否可在上游调用，以及提取间隔和后台日志 |

## 官方参考

- [Zeabur 快速开始](https://zeabur.com/docs/zh-CN/get-started/quick-start)
- [Zeabur Dockerfile 部署](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)
- [Zeabur 环境变量与变量引用](https://zeabur.com/docs/zh-CN/deploy/config/environment-variables)
- [Zeabur PostgreSQL 服务与连接串](https://zeabur.com/zh-CN/templates/B20CX0)
- [Zeabur 公网域名与端口](https://zeabur.com/docs/zh-CN/deploy/networking/public-networking)
