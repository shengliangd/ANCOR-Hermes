# ANCOR

ANCOR（AI-Native Coordination Runtime）把长时间、跨步骤的工作，从一次性对话变成可确认、可追踪、可验收、自动调度执行的正式请求。

## ANCOR 是什么

普通聊天适合快速问答，但面对持续数小时、涉及多个工具和外部系统的多项工作，常见问题是：

- **意图越来越模糊**：用户要求、执行方法和临时决定混在聊天记录里，范围容易漂移；一般性同意也可能被误认为具体授权。
- **协作依赖用户居中协调**：各会话不知道彼此正在占用哪些机器、目录、GPU 或外部配额，冲突需要用户发现；同时 agent 又可能为琐碎问题反复打断用户。
- **交付缺少可信状态**：agent 说“完成了”，但没有独立验证；进度、问题和最终验收也没有形成稳定、可检查的统一状态。

ANCOR 用“正式 request”解决这些问题。Request 是一个稳定的承诺边界，包含：

- **目标（outcome）**：用户最终需要得到什么；
- **约束（constraint）**：哪些边界不可违反；
- **建议（advice）**：可选的方法性建议；
- **背景（context）**：准确路径、对象清单、格式、环境事实和消除歧义所需的细节。

请求的创建、修改和验收都需要明确的人类确认。执行计划、任务拆分、过程日志和验证证据不会混入 request 正文，而是进入 coordinator 的任务卡和验证记录。

## 工作方式

ANCOR 将工作分成三个角色：

### Interactor

直接面对用户，负责：

- 处理轻量问题；
- 在用户执行 `/ancor create` 后发起正式需求；
- 进行必要的 intake 和澄清；
- 记录用户回答、授权、暂停、恢复、取消和最终验收；
- 用简洁的方式解释当前状态。

Interactor 不会自动把普通需求变成正式 request。正式创建必须由用户显式发起。

### Coordinator

负责执行阶段：

- 调研并验证 request 中不确定的部分；
- 将工作拆成有明确契约的 Kanban 卡片；
- 协调多个 worker；
- 根据新问题提出带确认的 request 修订；
- 独立验证交付结果并发送完成通知。

### Worker

每次只处理一张有边界的任务卡，在授权范围内使用工具、产出文件和证据，并在完成、阻塞或需要输入时返回明确结果。

这种分工让人类意图、执行自由度和验收责任保持分离：

```text
用户确认 request
      ↓
Coordinator 规划和协调
      ↓
Worker 执行单个任务
      ↓
Coordinator 验证交付
      ↓
用户最终验收
```

### 减少对用户的干扰

ANCOR 不会把每次重试、任务切换、worker 心跳或中间检查都推给用户。Interactor 只在 intake 阶段询问会改变结果、范围或授权的问题；Coordinator 在执行中自行处理可逆的技术选择、失败恢复、任务顺序和常规资源分配。只有当继续执行需要新的授权、改变用户已设定的取舍，或出现无法依据现有约束解决的冲突时，才会向用户提出一个聚焦的问题。

用户不需要持续盯着每个 worker。可以随时用 `/ancor list` 查看所有未关闭 request、当前状态和真正需要自己处理的动作；其余工作继续推进。

### 协调资源冲突

多个 request 同时运行时，Coordinator 看到的是同一张 Kanban 和同一组已授权资源。它可以根据依赖关系调整任务顺序，暂缓冲突卡片，避免同一 GPU、机器、挂载目录、上传通道或外部配额被相互破坏的任务同时占用。

资源冲突如果能通过排队、拆分、降低并发或调整依赖解决，就不需要打扰用户。只有当冲突要求突破现有授权、改变优先级，或在相互冲突的业务目标之间做取舍时，Coordinator 才会请求用户决定。ANCOR 不会凭空创造资源，它负责把已有资源用得更协调、更可解释。

## 当前实现：Hermes 插件

ANCOR 当前实现为 [Hermes](https://github.com/NousResearch/hermes-agent) 的一个插件：

- 持久化 request、问题、通知、任务关联和验证证据；
- Interactor、Coordinator、Worker 三类 profile 的 ANCOR 工具接口；
- 基于 Hermes 原生 `clarify` 的正式确认流程；
- 基于原生 Kanban 的任务编排；
- Gateway 轮询、事件唤醒和通知投递；
- 只读的 `/ancor` dashboard；
- 仅安装给 interactor 的 `/ancor` 技能命令。

仓库同时提供三角色的 SOUL、SKILL 和安装器：

- [`roles/`](roles/)
- [`skills/`](skills/)
- [`ancor/plugin.yaml`](ancor/plugin.yaml)

因此，使用 ANCOR 需要一个已配置好的 Hermes Gateway、原生 Kanban、`clarify`、插件 hooks，以及可运行多个 profile 的环境。

## 安装

完整安装、profile、模型、工具集、消息通道和验证步骤见 **[SETUP.md](SETUP.md)**。

ANCOR 需要同时配置 owner、interactor、coordinator 和 worker 多个 Hermes profile。建议直接让常用的 coding agent 阅读并执行 `SETUP.md`，先检查现有 Hermes 配置，再创建缺失 profile、安装插件、应用角色提示并完成验证。可以这样发起：

```text
请阅读 ANCOR-Hermes 仓库中的 SETUP.md，为当前 Hermes 安装 ANCOR。
先检查已有配置，不要覆盖我未要求修改的 profile、凭据或消息路由。
安装完成后按 SETUP.md 的检查清单验证。
```

初始配置建议让 interactor、coordinator 和 worker 统一使用 **DeepSeek V4.1 Flash high** 或 **GPT-6.1 Sol medium**。先把完整流程跑通，再根据质量、延迟、成本和并发需求分别调整各角色模型。

## 使用

Interactor 支持以下命令：

| 命令 | 作用 |
| --- | --- |
| `/ancor create <需求>` | 开始创建一条正式 request |
| `/ancor revise <request> <变更>` | 修改已有 request |
| `/ancor list` | 查看全部未关闭 request 及下一步操作 |
| `/ancor show <request>` | 查看一条 request 的当前状态 |
| `/ancor answer <question> <回复>` | 回答 coordinator 的开放问题 |
| `/ancor accept <request>` | 验收最新已验证交付 |
| `/ancor help` | 显示命令帮助 |

暂停、恢复、取消、授权、撤回授权和发送一般信息仍然可以通过明确的自然语言完成。

### 一个完整例子

假设用户想把一批原始 CSV 清洗成可训练数据，并生成质量报告；同时另一个 ANCOR request 正在使用同一台处理机和共享挂载目录。

#### 1. 显式创建正式 request

```text
/ancor create 将 /data/raw 下的 12 个 CSV 清洗、去重，生成可训练数据集和质量报告。原始数据不能修改。
```

`/ancor create` 只表示开始正式需求，不代表创建已经生效。

#### 2. Interactor 先做 intake 和澄清

Interactor 会先检查能自行确认的信息，然后只询问会改变结果或授权边界的问题，例如：

```text
需要确认三点：
1. 清洗后的数据是否输出到 /data/clean/v1？
2. “重复”按整行完全一致判断，还是按 user_id + timestamp 判断？
3. 被过滤的记录是否要单独保存以便审计？
```

用户回答后，interactor 才会整理 request。

#### 3. 确认 request

工具会展示一份接近下面的正式内容，并通过 Hermes 原生 `clarify` 让用户确认：

```text
标题：清洗 12 个 CSV 并生成可训练数据与质量报告

目标：
- 将 /data/raw 下 12 个 CSV 清洗、去重并生成可训练数据集。
- 输出数据质量报告，包含输入量、保留量、过滤量和过滤原因。

约束：
- 原始数据只读，不得修改。
- 重复按 user_id + timestamp 判断。
- 被过滤记录单独保存，便于审计。

Context：
- 输入：/data/raw
- 输出：/data/clean/v1
- 异常记录：/data/clean/v1/rejected
```

只有用户确认后，request 才会真正写入 ANCOR 存储。

#### 4. Coordinator 和 Worker 执行

Coordinator 会把工作拆成有边界的 Kanban 卡片，例如：

- 校验 12 个 CSV 的结构和编码；
- 实现清洗、去重和异常记录；
- 生成质量报告；
- 独立验证输出和约束覆盖。

Worker 在任务卡范围内执行，不会自行扩大 request 授权。

此时 Coordinator 发现另一个 request 正在使用同一台处理机和共享挂载目录。它先运行不冲突的结构校验和报告准备，把重清洗任务排队，等资源释放后再开始，不需要为此打断用户。如果两个 request 都带硬期限且无法安全排序，Coordinator 才会提出一个具体的优先级问题。

#### 5. 查看进度与验收

```text
/ancor list
```

资源冲突被自动排开时，用户看到的是：

| | 需求 | 当前状态 | 需要你做什么 |
|---|---|---|---|
| 🟢 | 清洗 12 个 CSV（abc12345） | 等待共享处理机释放，协调者已自动排队 | 无需操作 |

如果执行中发现 `timestamp` 存在多种格式，影响“是否属于同一记录”的定义，而现有 request 没有覆盖这个新歧义，Coordinator 会暂停相关工作，只提出这个必要问题：

| | 需求 | 当前状态 | 需要你做什么 |
|---|---|---|---|
| ⏳ | 清洗 12 个 CSV（abc12345） | 等待确认时间戳解析口径 | `/ancor answer <问题ID> <回复>` |

交付完成并通过 coordinator 验证后：

| | 需求 | 当前状态 | 需要你做什么 |
|---|---|---|---|
| 🔵 | 清洗 12 个 CSV（abc12345） | 待验收 | `/ancor accept abc12345` |

用户可以查看详情后验收：

```text
/ancor show abc12345
/ancor accept abc12345
```

执行完成、coordinator 验证和用户验收是三个不同阶段。只有最终验收会把 request 标记为 accepted。

## 设计原则

- **Request 是承诺边界，不是项目流水账。** 进度、任务拆分和证据属于执行记录。
- **Command 不是确认。** `/ancor create` 只启动流程，正式提交仍由 native `clarify` 完成。
- **人类授权必须具体。** 一般性偏好或聊天同意不会被自动解释成权限。
- **执行与验收分离。** Worker 完成、Coordinator 验证和用户 acceptance 是三件事。
- **只在关键节点打断用户。** 常规重试、排队和资源冲突由 Coordinator 处理；只有新的授权、取舍或实质歧义需要用户回答。
- **资源冲突优先协调，而不是升级给用户。** 在现有授权内通过排序、排队和限流解决；需要突破边界时才请求决定。
- **上下文按需展开。** 默认状态保持紧凑，完整要求、历史、问题和任务证据按需读取。

## 文档

- [安装与配置](SETUP.md)
- [Request 模型和事务语义](docs/atomic-requests.md)
- [Request 编写标准](skills/ancor/references/REQUEST_STANDARD.md)
- [插件设置与接口](ancor/plugin.yaml)
- [隔离真实模型 trial](scripts/isolated_trial.py)

## 开发

使用已安装 `pytest` 的 Python 环境，在仓库根目录运行：

```sh
env -u PYTHONPATH python3 -m pytest tests -q
```

部分 native integration 测试需要兼容的 Hermes checkout 和依赖；条件不满足时会跳过。可以通过 `ANCOR_HERMES_SOURCE` 指定 Hermes 源码路径。

## License

ANCOR 使用 [MIT License](LICENSE)。
