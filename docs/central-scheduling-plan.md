# CPU 核心组集中调度实施计划

状态：CPU 核心代码、workflow 和配置模板已在 `Recycle-the-deployment-of-light` 分支实现，待部署验收。工作区版本标记为 v2.0.0，未创建或推送发布标签。没有安装或重启现有服务，没有更名、注销或注册运行中的 runner。

1. **范围与版本边界**

   plan 主机负责构建仓库 plan、维护 CPU/GPU 队列、派发、回收和汇总。CPU 以 4 个逻辑 CPU 加 4G 内存为一组；runner 与组没有固定绑定，一个任务取得若干组并占用一个 runner。内存的 `G` 沿用现有 GiB 口径。

   本轮实现私有仓库的 CPU 调度；GPU 先保留独立列表和队列，一块 GPU 一组，执行规则另定。公开仓库不因此取得私有资源池访问权限。

   最终撤销私有流程中的 light、home、nightly 专用 runner／池／标签。既有 nightly 定时触发保留，其测试和普通 PR 测试使用同一批编号 runner。home 的本地恢复功能由常驻执行服务接替。

   这些变化会改变旧 plan 默认值、标签、inventory 和执行语义，按 `v2` 契约及 plan `schema: 2` 实施；旧 `v1` 的含义不被静默改写。实施先在本分支完成，版本和主机切换是后续步骤。`docs/design.md` 的拟议修订作为契约入口。

2. **整体架构与状态归属**

   首版确定为一个中央调度服务、一个持久任务库、每台物理执行主机一个执行服务。plan runner、GitHub 执行 runner 和这两个常驻服务分别承担不同职责：

   ```mermaid
   flowchart TD
       Source["被测仓库：PR / 手动 / 定时 CI"] --> Plan["plan 主机：plan-0、plan-1 构建 plan"]
       Plan -->|提交任务和文件引用后结束| Scheduler["ci-plan.service：调度、派发、取消、汇总"]
       Scheduler <--> Store["持久任务库：组额度、runner、任务、分配、待发送事件"]
       Scheduler -->|预留组和 runner 后派发| Workflow["被测仓库 ci-execute.yml → 可复用 execute.yml"]
       Workflow -->|唯一 tag| Runner["编号 GitHub runner"]
       Runner -->|本机调用并等待结果| Executor["ci-executor.service：每台物理主机一个"]
       Executor -->|本地 CPU 组与内存限制| Local["现有 part 执行器 → 测试命令"]
       Executor -->|Slurm 授权额度| Slurm["现有 Slurm driver / gate → 计算节点"]
       Executor -->|确认、心跳、释放和结果引用| Scheduler
       Scheduler -->|pending 与最终结果| Status["原被测 SHA 的 CI 状态"]
   ```

   - **plan 构建入口**：初始两个 runner，限制同时运行的仓库 plan 命令数量；只负责读取 CI 规则、生成并提交任务。资源不足的测试任务排在中央任务库，不占用 plan runner 等待。plan 命令自己的 CPU／内存预算单独配置，不计入 58 个测试组。
   - **中央调度服务**：全局只有一个活跃的分配写入者，拥有组额度与 runner 预留的最终决定权。首版采用 plan 主机本地的 SQLite，通过 Python 标准库 `sqlite3` 访问；事务显式提交和回滚。SQLite 不需要独立数据库服务，符合当前仓库无运行时第三方依赖的结构，见 [Python 文档](https://docs.python.org/3.12/library/sqlite3.html)。数据库访问封装在 `scheduler_store.py`，其他主机不直接打开数据库文件。
   - **本机执行服务**：每台物理主机一个，服务该主机的所有编号 runner，管理本地 CPU 组、进程和回报。如果一台主机同时承载本地 runner 与 Slurm driver，也共享一个执行服务，两个后端的额度分别记账。runner 的 step 调用服务并等待日志／结果；服务复用现有 `part.py` 监督逻辑启动命令，保持单一启动路径。
   - **GitHub 与结果存储**：GitHub 负责事件、按唯一 tag 投递 job、展示日志和保存 artifact。调度服务存储结果索引并汇总小体积结果文件，完整日志和测试产物不经过调度事务或常驻内存转发。

   | 记录 | 权威位置 | 内容与约束 |
   |---|---|---|
   | 资源池与 runner 登记 | 中央配置和任务库 | 组总数、runner 总数、唯一 tag、所属资源池、物理主机、后端、健康与 drain 状态；实际地址和凭据只在主机配置 |
   | 批次、任务与分配 | 中央任务库 | 源仓库／SHA／run／attempt／并发键、需求、排队期限、所选组数与 runner、执行 run、状态及结果引用；占用数量从未释放的分配记录统计 |
   | CPU 编号与进程 | 本机执行服务的持久记录 | `allocation_id → CPU 组／CPU 集合／任务 scope／进程身份`；中央只收取确认与摘要，不负责选择具体 CPU 编号 |
   | Slurm 作业 | driver 持久记录及集群 | `allocation_id → Slurm job ID`、排队／运行／取消状态及实际资源；Slurm 分配计算节点的 CPU 编号 |
   | 待发送的派发与回报 | 发送方持久记录 | 先记账后发送，按分配 ID 去重；服务重启后重送和核对，不靠一次网络响应判定资源已释放 |

   本机 runner 通过 Unix socket 调用本机服务；执行服务通过经过身份认证的 HTTPS 主动访问 plan，领取／核对授权、发送事件，并在心跳响应中取得取消指令。执行主机不需要对 plan 开放新的入站控制端口。中央派发凭据与数据库不交给执行仓库代码的 plan runner；沿用并扩展现有 hook 的身份核验。连接地址、凭据和本地 socket 权限属于部署配置。

   数据更新保持短事务：一次事务同时写入组预留、runner 预留和待派发记录，提交后才调用 GitHub。接收回报只做幂等状态更新；网络、文件传输和测试执行均在事务外。按 PR 轮流选择可运行任务，已预留及尚未完成的执行任务计入项目并行限制，防止只统计已启动任务而过量派发。

   对于同时 3～4 个 PR、每个 8 个任务，中央管理的是 24～32 条任务记录与相应事件；测试负载仍在执行设备。容量不足表现为任务排队。4 个 PR 的 32 个任务若都申请 1 组，理论上低于本地合计 38 组；设备限制、已有占用和项目并行限制仍可能导致等待。这是容量推算，不替代 plan 构建耗时、API 派发延迟和并发验收。

   plan 主机停机时暂停新分配；已启动任务由本机服务继续监督、限时和清理，回报落盘后补发。恢复时先核对未结束分配，再恢复派发。首版接受这一单点带来的调度暂停，数据库备份与恢复步骤列入运维交付。

3. **确定 runner 名称、tag 和内部角色**

   tag 指 GitHub runner 的自定义 label。runner 名称与它的唯一 tag 相同；普通主机从 0 编号，使用 `<设备>-<序号>`。按本次约定，Slurm 的准确拼写为 `slurm0`～`slurm19`，不带连字符。H100 在配置和标签中统一小写为 `h100`。

   | 用途／资源池 | CPU 组数 | runner 名称与唯一 tag | 额外共享 tag | GitHub 注册数量 |
   |---|---:|---|---|---:|
   | plan 主机 | 不计入测试 CPU 组 | `plan-0`、`plan-1` | `ci-plan` | 初始 2 |
   | 3090a | 4 | `3090a-0`～`3090a-3` | 无 | 4 |
   | 3090b | 4 | `3090b-0`～`3090b-3` | 无 | 4 |
   | pro6000 | 20 | `pro6000-0`～`pro6000-19` | 无 | 20 |
   | h100 | 10 | `h100-0`～`h100-9` | 无 | 10 |
   | Slurm | 20 | `slurm0`～`slurm19` | 无 | 20 |

   合计 58 个测试 CPU 组、232 核、232G，58 个执行 runner；初始另有 2 个 plan runner。Slurm runner 注册在可访问集群的驱动设备上，其物理位置不从名称推测，也不重复计入该驱动设备的本地 CPU 组数。

   GitHub 自动附加的 `self-hosted`、`Linux`、`X64` 等系统标签保留，以实际平台为准。新 CPU 流程只使用以下自定义标签：共享的 `ci-plan`，以及上表各 runner 的唯一 tag。被测仓库不直接选唯一 tag。

   ```yaml
   # 构建 plan 的 job：只有 plan 主机有 ci-plan
   runs-on: [self-hosted, ci-plan]
   ```

   ```yaml
   # 执行 job：runner_tag 由调度器从登记表选出
   runs-on: [self-hosted, "${{ inputs.runner_tag }}"]
   ```

   新 CPU 池不带 `ci-cpu`、`ci-cpu-dynamic`、`ci-cpu-N`、`ci-slurm` 或设备级共享 CPU tag，避免 GitHub 绕过组分配自行选择 runner。`ci-light`、`<设备>-light`、`<设备>-home`、`<设备>-nightly` 以及相应专用 runner 在切换后全部退出；nightly 仍可作为工作流或任务名称，不能作为资源类别。

   新私有 CPU 流程的内部 `CI_RUNNER_CLASS` 使用 `plan`、`executor`；执行后端另记为 `local` 或 `slurm`。runner 记录显式保存 `runner_id`、唯一 tag、所属资源池、驱动主机及后端，替代按 runner 名称反推主机。现有公开 GPU 的权限与类别分支不因 CPU 重构被直接删除；其迁移另行处理。

4. **被测仓库的输入：新增可选 `device`**

   `device` 放在仓库生成的 `parts[]` 中，一个测试部件可以独立指定设备。外层 workflow 的 job 名称不作为设备依据；`files` 与 `max_parallel` 继续位于 plan 顶层。

   | 字段 | 新协议约定 |
   |---|---|
   | `needs` | 本轮 CPU 任务为 `cpu`；不接受 `light`、`home`、`nightly` 作为新任务类型 |
   | `device` | 可省略或为 `""`，表示自由调度；非空枚举为 `slurm`、`pro6000`、`3090a`、`3090b`、`h100` |
   | `cores` | 省略默认 4；显式值为 4 的正整数倍；不隐式修改请求 |
   | `workers` | 仓库显式提供正整数，按原值传给执行端，不根据组数重新计算 |
   | `minutes` | 仓库声明本次命令允许运行的分钟数，普通任务和 nightly 使用同一字段 |
   | `queue_minutes` | 等待资源的期限；与命令运行期限分开统计，重新派发不无限重置期限 |
   | `command`、`results`、`artifacts`、`history` | 保留各测试部件的原有用途，适配独立执行 run 的传递和结果汇总 |
   | 顶层 `files` | 保留 plan 生成文件的传递，由源 run 的文件引用关联到执行 run |
   | 顶层 `max_parallel` | 可降低项目登记的单批次并行上限，不能调高；中央按预留及尚未完成的执行任务计数，不等同于 CPU 组数 |

   被测仓库可以通过 `device: "slurm"` 指定 Slurm 资源池，具体 runner 仍由 plan 选择。`device` 不接受拼写错误的 `slum`，也不接受 runner tag。传入未识别的设备、大小写变体或 `null` 时给出明确错误，不静默回退为自由调度。

   - `device: ""`：在符合环境、权限、额度及期限的资源中选择，默认先使用本地设备，再考虑已启用且预算允许的 Slurm。允许重试时重新选择设备。
   - 指定设备：只在该设备或资源池取得组和 runner，缺资源时在 plan 排队，超过期限后报告资源不可用。重试也保留设备要求，不改投其他资源池。
   - `device: "slurm"`：只选择 `slurm0`～`slurm19`，不回退到本地设备；仍须通过集群启用状态、账户准入、预算与资源上限检查，显式指定不能绕过这些限制。
   - 请求超过指定设备总额度、或自由调度时超过所有允许后端的单任务上限，在 plan 阶段拒绝。

   例如，被测仓库可生成如下 plan；这里用 600 分钟说明长时任务仍是普通 CPU 任务：

   ```json
   {
     "schema": 2,
     "parts": [{
       "id": "linux-coverage",
       "needs": "cpu",
       "device": "pro6000",
       "cores": 12,
       "workers": 8,
       "minutes": 600,
       "queue_minutes": 15,
       "command": "python -m pytest -n \"$CI_WORKERS\" brainmass/",
       "results": "pytest.xml"
     }]
   }
   ```

   `cpu_groups`、`RAM`、实际 runner 和分配 ID 由 plan 计算，不能由被测仓库伪造分配。保留原始设备请求和实际资源池两项，便于解释自由调度结果。内部执行授权示例：

   ```json
   {
     "allocation_schema": 1,
     "task_id": "brainmass-pr42-linux-coverage",
     "allocation_id": "allocation-001",
     "requested_device": "pro6000",
     "device": "pro6000",
     "backend": "local",
     "host": "executor-example",
     "runner_tag": "pro6000-9",
     "cpu_groups": 3,
     "cores": 12,
     "RAM": "12G",
     "workers": 8,
     "minutes": 600,
     "queue_minutes": 15,
     "job_timeout_minutes": 645
   }
   ```

   自由调度选到 Slurm 时，`requested_device` 为 `""`；仓库显式指定 Slurm 时，该字段为 `slurm`。两种情况下，内部 `device` 和 `backend` 都记为 `slurm`，`runner_tag` 取 `slurm0`～`slurm19` 中的一个。命令、被测提交、源 run、执行 run 和 artifact 对应关系另存于同一任务记录。

5. **nightly 与 home 的处理**

   **nightly 不保留专用 runner。** 日程触发方式仍由被测仓库决定；plan 的 `minutes` 控制执行时长。执行器统一采用任务运行上限，外层 GitHub job 的 `timeout-minutes` 另外包含执行前等待、环境准备和收尾预算。需要同时修改现在的 240 分钟 plan 校验、按类限制的 600 分钟上限、Slurm 时间限制、回收等待及记录保留期限，不能只修改 workflow 的一处超时。

   首版保留覆盖原 600 分钟任务的能力，但上限改由项目与后端配置限制，不由 runner 名称决定。GitHub job 和 step 的限制不同，长时任务不能给单个 step 填超限的 `timeout-minutes`；模板需显式设置 job 期限，测试命令由执行器计时。平台边界以 [GitHub workflow 语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idtimeout-minutes)为准。

   现有 `part.py` 的 nightly 还会取得全部 CPU 槽位和全部 GPU 租约。新协议取消这一隐式行为：长时间运行只按声明的组数占资源。旧任务若依赖 CPU 独占，需要显式指定设备并申请该设备全部 CPU 组；这仍不等于独占 GPU 或禁止非 CI 用户使用主机。依赖整机／GPU 独占的旧测试单独列入迁移清单，不仅改名后直接放行。

   **home runner 不保留，恢复服务保留。** 每台执行设备运行常驻 `ci-executor.service`，负责 CPU 组占用表、任务监督、状态回报、取消清理及重启核对；其工作不依赖空闲的 GitHub runner。plan 经这条控制通道取得本地结果，替代 `<设备>-light` 上的 salvage job。

   旧 `presence hold` 用于阻止低优先级 runner 被停止；新 CPU runner 不再因层级或组耗尽而反复停启，设备选择由 plan 完成，因此新流程移除 home 专属的 hold 路径。drain 改为停止新分配，仍允许执行服务回报、清理和回收。只有新恢复通道验收通过，才撤下旧 home runner。

6. **分配、执行与释放的闭环**

   持久记录包含源仓库、提交 SHA、源 run/attempt、部件 ID、原始需求、分配 ID、所选资源池、runner、执行 run、状态和结果。任务身份包含源 run/attempt，避免同一 PR 的不同提交互相覆盖。

   1. plan 校验并持久接收整批任务，确认所需 plan files 已上传且记录可读取后才允许派发，随后结束构建 job；测试结束由异步状态回写体现，提交 plan 成功不等于测试通过。中央服务按 PR 轮流取任务，并遵守项目的并行任务上限。
   2. 在同一事务内预留足够 CPU 组和一个在线可用 runner。`可分配组数 = 总组数 - 预留组数 - 已分配组数`；确认等待、清理和失联待核实的额度都不能重新分配。
   3. 以唯一分配 ID 提交一个执行 workflow。派发结果不明时先核对，不另起一次无关分配；网络请求在短事务外执行。
   4. 执行端验证授权绑定的仓库、SHA、资源、runner 和分配 ID；在本地锁内取得互不重叠的四 CPU 组，设置合计 RAM 上限，回报成功并运行命令。重复启动同一分配只返回已有状态。只要求 CI 之间不重叠，不根据外部用户的 CPU 使用情况扣减组数。
   5. CPU 组确认由预留转为已分配，不二次扣减。测试命令及其子进程均受 CPU 集合与内存额度约束；`workers` 原样进入 `CI_WORKERS`。
   6. 任务结束、超时或取消后，先清理进程，再释放 CPU 组并回报。runner 仍在上传／收尾时保持不可选；组可以先供本机其他空闲 runner 使用。
   7. 回报重复、乱序、丢失或服务重启时，以分配记录和本地执行证据核对。源 CI 取消要传递到执行 run；不能仅因 GitHub 标记 cancelled 就认定本地资源已清理。对于调用方已有的 `cancel-in-progress: true`，同一 PR 新提交还需按源并发键取消上一批未完成任务，即使旧的 plan 入口 run 已经结束；不能仅依赖 GitHub 取消入口 job。
   8. plan 汇总最后一次有效尝试的结果，回写被测 SHA 的最终状态；源 run 只承担提交职责，后续依赖其“完整测试结束”的调用需要迁移为等待最终状态或读取汇总结果。

   GitHub 执行入口设在被测仓库：增加一个很短的 `workflow_dispatch` 包装 workflow，调用本库 `workflow_call` 类型的执行模板。这样保留被测仓库身份，也不让 `ci-runners` 自己的事件运行自托管 job，符合现有 hook 的 self 拒绝规则。分配 ID 和执行参数作为输入传递，不能把任意外部传入命令视为有效授权。入口与调用关系参考 [GitHub 可复用工作流说明](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations)。

   包装入口先进入被测仓库默认分支，再由调度器从受信任的 workflow ref 派发；GitHub 要求 `workflow_dispatch` 入口存在于默认分支，而且该事件的 `github.sha` 对应派发的分支／标签，见 [GitHub 事件文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)。因此任务必须显式 checkout 分配记录中的被测 SHA，不能直接测试派发事件的 SHA。hook 同时核对源事件身份与本次分配，不能把服务账号发起 dispatch 当作原提交者的准入证明。

   包装入口的输入确定为 `runner_tag` 与 `allocation_json`；后者显式带上分配 ID、CPU 组数、cores、RAM、workers 和时长，便于在执行 run 中查看。执行端逐项对照中央授权，不信任调用方自行增加的额度；命令与文件引用从绑定的任务记录取得。

   v2 入口输出批次标识 `batch_id` 和提交状态 `submission_state`，旧 `state` 不再承担最终测试结论。仓库现有最终状态 context（例如 `brainmass/linux-coverage`）从接收批次开始保持 pending，全部有效尝试结束后才更新最终结论；需要同步盘点必需检查和后续工作流。`plan-only` 保持只验证、不分配、不派发，使用独立状态 context。

7. **Slurm 接入保持 20 组、20 个编号入口**

   `slurm0`～`slurm19` 是 GitHub runner 名称及唯一 tag，登记在一个或多个驱动主机上；20 组是集群侧的统一额度。一个任务占 `g` 组和一个 Slurm runner，提交 `4g` CPU、`4gG` RAM，不借用驱动主机的本地测试 CPU 组。

   账户准入、预算、分区容量、单节点资源和时限仍需满足。提交后排队期间保留额度，节点真正获得并落实资源后才确认分配；取消要核实远端进程已经结束。具体 CPU 编号由 Slurm 给出，不由 plan 在不同计算节点之间统一编号。

   改造当前按 target 固定取 CPU 数的实现，让 gate 读取并验证本次授权；`workers` 传入远端命令，取消现有“Slurm 不支持显式 workers”的路径。仍保留 gate 校验、日志脱敏、计费、远端取消和结果同步。驱动进程的本地内存与连接开销单独限制，不冒充集群 CPU 组。

8. **确定修改面**

   当前分支的实现修改面如下。共享的 v1/GPU 模块保留旧入口；新 CPU 路径通过 schema 2 和独立入口启用。

   | 修改区域 | 已实现文件／入口 | 行为 |
   |---|---|---|
   | 契约与迁移 | 本文、`design.md`、`consumers.md`、`operations.md`、`AGENTS.md` | v2 标签、资源与状态契约，部署顺序和退役条件 |
   | 资源与命名 | `central.py`、`inventory.py`、`registry.py`、`setup.py` | schema 2、固定组额度、编号 runner、device、workers 与时长校验；schema 1 继续可读 |
   | 中央队列 | `scheduler.py`、`scheduler_store.py`、`dispatch.py`、`control.py` | SQLite 原子预留、轮流调度、设备约束、持久派发队列、源身份校验、取消、结果汇总 |
   | 本机控制 | `executor_service.py`、`cpu_groups.py` | 精确 CPU 组、启动互斥、心跳、清理证明、持久回报；组和 runner 分开回收 |
   | 资源执行 | `allocated_part.py`、`part.py` | 复用现有 supervisor 和环境；scope 的 CPU 集合和 RAM 校验通过后才启动测试；本地账本阻止释放后迟到启动 |
   | plan 桥接 | `central_flow.py`、`actions/central/action.yml`、`cli.py` | prepare/build/submit/inspect/fetch/execute/capacity/batch；plan 命令无服务写凭据 |
   | 可复用 workflow | 新增 `ci-v2.yml`、`execute.yml` | ci-plan 提交后退出；独立执行 run 用唯一标签及原始 SHA；旧 `ci.yml` 保留 schema 1，新旧入口拒绝混用 |
   | 准入和存活 | `policy.py`、`hook.py`、`hooks/job-started.sh`、`presence.py` | 编号执行器先核对中央授权和原始源身份；plan/executor 不参与旧 CPU presence 停启 |
   | Slurm | `slurmgate.py`、`slurmpart.py`、`part.py` | 授权 cores/RAM/workers，按申请核心计算预算；真实资源就绪后 ACK，远端清理确认后释放；保留现有 gate、预算与脱敏 |
   | 安装模板 | `setup/units/ci-executor.service`、`setup/central/ci-plan.service`、`setup/central/*.example.toml` | executor 随新池的 setup plan 列出；plan 服务由隔离账号安装，模板不含真实凭据或机器连接信息 |
   | 调用方 | `setup/consumer-ci-execute.yml`、`setup/central/brainmass-v2.patch` | brainmass 的两个 CPU workflow 迁移补丁及默认分支包装入口；补丁尚未应用到它的 main |
   | 版本和验证 | `stamp.py`、`tests/test_central*.py`、`tests/central_mutants.py`、既有回归测试 | 新工作流和包装模板随 v2 统一盖章；离线并发、恢复、身份、资源、Slurm 和故障注入验证 |

   brainmass 迁移补丁为普通 coverage 声明 4 cores / 4 workers，为 8 核 canary 声明 8 cores / 8 workers，均设置 `device: ""`；worker 数是补丁中的显式初始值，可以按测试需要调整。brainmass 的 GPU workflow 以及其他仓库的现有 GPU/timing 路径先列清单，CPU 实施不擅自改成新的 GPU 分配策略。全量撤销 light 前，它们的辅助 plan／汇总／恢复入口也必须完成迁移。

   退役清单是旧 CPU 能力标签、固定 runner 槽位绑核、nightly 隐式整机租约、light/home runner、依赖它们的 salvage/hold 路径及 CPU 的 tier/presence 自动停启。共享模块按调用路径迁移，不因包含旧名称就整文件删除。GPU 联合分配策略、物理核／NUMA 拓扑优化、多台 plan 主机高可用、独立内存档位不纳入本轮实现。

9. **按阶段实现，每阶段有可检查的完成条件**

   | 阶段 | 交付内容 | 完成条件 |
   |---|---|---|
   | P0 契约与盘点 | 本计划、design 修订、旧调用清单、命名和 schema 2 | 标签和字段无歧义；明确 nightly 独占调用和完整 CI 状态依赖；代码实施按仓库契约评审流程推进 |
   | P1 数据与调度 | 登记 schema、SQLite 分配表、控制接口、队列和选择逻辑 | 离线模拟多个 PR 同时竞争最后几组，组数与 runner 均不重复预留，指定 device 始终保留；事务中断和重启后核对通过 |
   | P2 本地执行服务 | 动态 CPU 组、内存额度、回报、取消和重启核对 | 4／12／16 核混合执行，CPU 集合不重叠；重复消息、进程残留、断联均不提前释放 |
   | P3 GitHub 闭环 | plan 提交、源仓库包装入口、独立执行 run、最终状态 | 在一台设备的 canary 中完整运行，测试显式使用原 SHA，提交成功不会提前变成测试通过，重复派发不重复启动，取消能传递 |
   | P4 Slurm 与长时 | 20 组额度、编号入口、动态资源、minutes 全链路 | 自由调度及显式指定 Slurm 均通过准入和预算检查，指定 Slurm 不回退本地；集群排队不超额，远端 worker 和资源参数正确；模拟 600 分钟任务的超时传播；完成真实长时 canary |
   | P5 调用方迁移 | brainmass CPU、nightly、其余辅助流程及旧能力标签调用 | CI 最终结论仍对应被测提交；长时任务不申请 nightly runner；新流程没有 light/home 调度依赖 |
   | P6 主机切换 | 安装服务、登记新 runner、排空旧分配并撤销旧注册 | 所有相关调用方已迁移；home 的恢复替代已验收；新注册只含本计划的 CPU/plan 标签；组账本与本地状态一致 |

   不进行同一 CPU 集合上的新旧调度器双写。试运行时划出不重叠的 CI CPU 集合；正式切换时停止新接单、等待或取消并清理旧任务、核对资源，再切换。回退也先排空新分配，不能只恢复旧标签就把占用账本清零。

10. **验证与交付证据**

    既有 inventory、setup、route、flow、part、presence、drain、policy、hook、Slurm 和 artifact/verdict 测试继续作为兼容性回归。新的调度、CPU 组、服务、授权和恢复测试集中在 `tests/test_central.py`、`tests/test_central_services.py`；`test_actions.py`、`test_reusable.py` 同步验证 v2 版本和新模板。

    必须覆盖 device 的省略／空值／各合法设备（含 slurm）／slum 等非法值，重试不丢设备约束，显式指定 Slurm 不回退本地且不能绕过准入和预算，准确生成 58 个唯一执行 tag；minutes 在普通及定时触发中一致；4 个 PR 共 32 项任务的原子预留、重复派发、迟到回报、取消清理、重启、Slurm 排队以及 CPU 已释放但 runner 尚在收尾。

    按仓库规则记录相关测试、关键变异测试、每台主机的 setup dry-run 和现场 canary 结果。全量回归使用 `tests/leaks.py` 与 unittest，第一轮 1,453 项结果 OK（1 项跳过）；第二轮 1,462 项仅文档 lint 失败，修复后最终相关回归 339 项全部通过。inventory lint 包含已跟踪和新增文件。关键变异测试由 `tests/central_mutants.py` 加入现有 `tests/mutants.py`，最终 53/53 被测试捕获；实际证据见 [验证记录](central-verification.md)。

    发布前完成旧调用方扫描、全部恢复通道验证和长期任务的实际限制核对。主机注册、注销、服务安装及主版本发布按 [AGENTS.md](../AGENTS.md) 的主机操作流程列入实施交付；当前分支开发不执行这些操作。

11. **实施边界与上线前核验**

    - 当前代码面向已准入的私有仓库 CPU；公开仓库 CPU 仍按原协议，GPU 调度尚未纳入新队列。GPU 辅助工作流迁移前不能注销它们仍依赖的 light/home。`homo` 不作为新调度标签，历史所谓 home 的恢复职责由执行服务承担。
    - 同时 3～4 个 PR、每个 8 项任务时，plan 服务保存 24～32 条任务记录并逐项派发；2 个 plan runner 只在构建期间占用。数据库事务内不等待网络，GitHub 写请求串行且至少间隔 1 秒；这个数量不需要增加分发主机。实际吞吐仍需用真实仓库和网络验收，不能把离线队列测试当成压测。
    - `queue_minutes` 是重试间累计的等待预算，分配 ACK 后的运行时长不消耗它。`ceil(minutes + queue_minutes + 30) <= 1440`，因此 600 分钟 nightly 合法；项目和集群目标可设置更低上限。执行 job 的时长显式设置，来自 [GitHub job 超时和凭据时限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idtimeout-minutes)。
    - 派发请求使用 `return_run_details` 保存执行 run ID，行为依据 [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。响应丢失时按 allocation ID 核对，不假定失败并释放；持续无法核实的分配保持占用，排障时恢复 GitHub 可见性和执行端连接，不能清空账本抢占资源。
    - Slurm 的账户预算和目标容量在现有 driver/gate 上再次校验，失败结束本次尝试；plan 的 Slurm 列表目前是统一的静态 20 组额度。上线前将目标的 cpus 上限、cgroup 约束及节点可用性配置好。没有实际落实 CPU 集合和聚合内存上限时测试拒绝启动。
    - 最终 commit status 由中央服务凭据写入。仅计划提交成功不会报告测试通过；执行 workflow 或上传失败也不会覆盖成 success。`batch` 查询保留每次 allocation 的执行 run 和 artifact 关联，源 run 的成功只代表提交。
    - 本轮没有运行真实 GitHub 派发、集群 sbatch、主机 setup apply 或 runner 注册。单机 4/12/16 核 canary、进程树取消、实际 Slurm 运行、600 分钟长时和源仓库必需检查迁移是部署验收项。
