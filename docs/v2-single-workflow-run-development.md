# v2 CPU 单次 workflow run 开发过程记录

本文件集中记录 CPU 核心组调度和单次运行改造的需求、决策、实现过程与验证证据。执行部署只使用 [从头部署手册](v2-single-workflow-run-rebuild.md)；代码契约以 [design.md](design.md) 为准。

截至 2026-11-19，本仓库实现和本地回归已完成；旧 staging 服务已停止，25 个旧 runner 已注销，旧运行目录与缓存已清理，源码恢复到部署前状态。固定版本部署、新身份注册、消费方切换和新架构的真实 GitHub/Slurm 验收待执行。本次工作目录是没有 Git 元数据的源码副本，未在其中创建提交、远程分支或发布标签。

## 1. 问题和目标

原有 CPU 调度按固定 runner 槽位分配资源，难以表达不同任务的 CPU 与内存需求。第一阶段引入中央任务库和 CPU 组，调度器为每次分配启动独立执行 workflow。四份测试因此会生成一条 plan run 和四条执行 run；plan run 提前成功仅表示计划提交成功。

本次目标是在同一次源 workflow run 中完成 plan、所有必需 CPU 测试、允许的重试和最终判定。一份 pytest 失败则整次 CI 失败；其他分支仍能结束并留下日志与产物，各份任务测试相同 SHA。

范围为已准入私有仓库的 schema 2 CPU 任务，保留本地和 Slurm 后端。schema 1、公开仓库和 GPU 的既有入口保持兼容。GPU 联合分配、NUMA 优化、多调度器高可用及独立内存档位不在本轮范围内。

## 2. 资源模型和职责

一个 CPU 组代表 4 个逻辑 CPU 和 4 GiB 内存。任务原子取得若干组与一个 runner；组与 runner 不固定绑定。ACK 只确认实际资源已经落实，不再次扣额。`workers` 由计划显式给出，执行端原样使用。

| 资源池 | 组上限 | 唯一 runner 标签 |
| --- | ---: | --- |
| 3090a | 4 | `3090a-0` 至 `3090a-3` |
| 3090b | 4 | `3090b-0` 至 `3090b-3` |
| pro6000 | 20 | `pro6000-0` 至 `pro6000-19` |
| h100 | 10 | `h100-0` 至 `h100-9` |
| Slurm | 20 | `slurm0` 至 `slurm19` |

这是契约容量，不表示全部已经部署。总上限为 58 组、232 核和 232 GiB；plan 资源另行配置。停机前 staging 使用 1 个 plan runner、4 个本地执行 runner 和 20 个 Slurm driver。

| 对象 | 负责内容 |
| --- | --- |
| plan/申请/判定 job | 构建计划、提交批次、申请任务分配、等待并读取最终结果；使用 `ci-plan` |
| scheduler | 单一活跃分配写入者；维护批次、额度、runner 预留、取消和最终状态 |
| 中央 SQLite | 保存源身份、任务、分配和状态发布队列；短事务完成资源决策 |
| executor | 每台物理主机一个；管理本地 CPU 集合、进程、远端 driver 和持久回报 |
| GitHub runner | 接收针对唯一标签的执行 job，调用 executor 并上传产物 |
| Slurm driver/gate | 按授权资源提交、核验、计费、取消和收集远端任务 |

本机使用 Unix socket，执行主机主动通过认证 HTTPS 与中央通信。网络、文件传输和测试执行均在数据库事务外；执行仓库代码不持有中央写凭据。scheduler 不可用时暂停新分配，executor 继续监督、限时、清理并保留回报，恢复后先核对再调度。

`device` 省略或为空时自由调度，先考虑本地再考虑允许的 Slurm；显式设备是跨重试保留的硬约束。`cores` 默认 4，必须是 4 的正整数倍。显式 Slurm 仍受账户准入、预算、目标资源和时限限制。旧 GPU 等调用方未迁移前，其依赖的辅助 runner 不能随 CPU 改造一起退役。

## 3. 最终执行链路

```text
同一次源 workflow run
  plan：构建计划、上传共享文件、登记批次并输出任务 matrix
  每个 matrix 分支：申请首次分配 → 执行 → 申请允许的重试 → 重试 → 任务判定
  verdict：等待所有必需任务，读取中央账本并报告最终结果
```

入口为 [ci-v2.yml](../.github/workflows/ci-v2.yml)，任务编排为 [cpu-task.yml](../.github/workflows/cpu-task.yml)，两次尝试共用 [execute.yml](../.github/workflows/execute.yml)。三者通过 `workflow_call` 连接。matrix 设置 `fail-fast: false`。

matrix 只携带任务身份和等待时限。每个分支先申请，scheduler 才能预留组与 runner；申请 job 返回 runner 与授权，后续执行 job 使用这些输出。申请 job 会等待分配，中央核对与资源回收由常驻服务负责，不依赖 plan runner 空闲。

任务身份包含源仓库、SHA、`source_run_id`、`source_run_attempt`、任务 ID 和任务尝试次数。每次分配有独立 `allocation_id`，绑定数字 `execution_job_id`。任务尝试次数与 GitHub run attempt 分开；执行 job 名使用稳定后缀，按具体源 run attempt 查询，重名歧义直接拒绝。

hook 根据可信服务中的 runner 分配核验源事件、run/attempt、job 和 runner。命令、SHA、资源从中央记录取得。进程清理后释放 CPU 组，GitHub job 上传和收尾完成后释放 runner；缺少证据时保留相应占用。

基础设施错误最多重试一次，pytest 断言失败不参与成功恢复。重试需要新的任务申请，目录和产物按任务尝试隔离。空计划成功且不展开 matrix；`plan-only` 不分配资源，使用独立状态 context。

完整重新运行创建新 run attempt 的批次；仅重跑失败 job 若沿用旧批次则拒绝，并提示完整重跑。新提交和取消不能复用旧授权或覆盖新批次状态。单分配超时只撤销其授权，已排入 GitHub 队列的 job 仍须等终结证据才能释放 runner。

`queue_minutes` 是各尝试累计的等待预算，ACK 后的执行时长不消耗它。执行 job 时限按 `ceil(minutes + queue_minutes + 30)` 计算，并受项目与平台上限约束。Slurm 的账户预算、容量和远端清理仍由原 driver/gate 再次核验。

## 4. 实现过程

| 阶段 | 修改位置 | 已完成行为 |
| --- | --- | --- |
| 资源建模 | `central.py`、`inventory.py`、`registry.py`、`setup.py` | schema 2、组额度、唯一 runner 标签、设备与 workers 校验 |
| 中央账本 | `scheduler_store.py`、`scheduler.py` | 公平调度、原子预留、幂等申请、累计等待预算；仅已申请任务可分配 |
| 执行与清理 | `executor_service.py`、`cpu_groups.py`、`allocated_part.py` | 明确 CPU 集合和聚合内存、启动互斥、清理证明及持久回报 |
| 源与 job 核验 | `dispatch.py`、`hook.py` | 从独立 run 核对改为源 run attempt 内逐 job 核对；保留原事件准入 |
| 工作流桥接 | `central_flow.py`、`actions/central/action.yml`、三个 workflow | 输出 matrix、申请分配、执行、重试和最终失败传播 |
| Slurm | `slurmpart.py`、`slurmgate.py`、既有 part 执行器 | 使用本次授权 cores/RAM/workers，按申请资源核算预算并确认远端清理 |
| 旧路径收敛 | `registry.py`、`stamp.py`、配置与消费方模板 | 移除独立 dispatch 包装和旧配置字段，新模板统一盖章 |
| 验证 | `test_central*.py`、`test_single_workflow.py`、既有回归与变异表 | 状态机、协议、hook、产物、恢复、模板及兼容性检查 |

数据库升级新增源结束状态、任务申请次数、任务尝试和 job ID。版本 1 只有在旧批次和分配排空后才允许升级到版本 2，并保留历史。当前部署决定为终止旧执行、归档旧记录并从空库重建；迁移能力仍保留在代码中，不能用于清库抢占运行中的资源。

可读性约束贯穿实现：workflow 表达依赖，scheduler 分配，executor 运行和清理，verdict 读取结果；资源额度、重试资格和最终状态各有一处权威判断。使用明确的对象/动作命名，例如 `wait_for_task_allocation`、`reconcile_execution_jobs`；提前返回减少嵌套，不为拆短函数增加只转发参数的包装层。

## 5. 开发中发现并修复的问题

| 问题 | 修正及验证 |
| --- | --- |
| central 输出复用 v1 固定字段写入器，新 matrix/授权字段未真正写出 | 使用明确的单行输出写入逻辑，检查字段名和换行；桥接回归及变异验证 |
| 重试目录已分开，但产物收集仍默认读取首次记录 | 收集时传入 `task_attempt`；验证第二次产物包含第二次 outcome、summary 和 job 身份 |
| GitHub 排队 job 尚无 runner 被当作错误 runner | 排队核对允许未指派 runner，执行中的 job 仍严格匹配；先复现失败，再通过回归和变异检查 |
| 取消测试使用错误源身份，其他条件先行拒绝，遮挡了取消检查 | 改为用同一合法身份验证取消后的拒绝 |
| 并发上限为 1 遮挡了 runner 提前复用问题 | 同时验证并发上限 1 和 3，确保 job 收尾条件独立生效 |
| 首轮全量测试受源码无 Git 元数据和 `umask 0002` 影响 | 在隔离 Git 测试副本中使用 `umask 022` 复验；未将测试仓库元数据加入源码目录 |
| 原先假定 release 与工作区隔离，与 staging 实际布局不符 | 现场发现 release 直接链接工作区，修正文档；部署改用固定版本目录 |

## 6. 本地验证证据

验证环境为工作站开发工作区、系统 Python 3.12.3。长测试使用共享主机锁及临时 systemd scope，限制 CPUQuota=100%、MemoryMax=2G、MemorySwapMax=0。执行测试使用临时目录与替身，不代表真实 GitHub/cgroup/Slurm 验收。

### 集中调度阶段的历史记录

以下从原验证记录归并，本轮文档整理没有重新执行：

- 首次全量 1,453 项，OK，1 项跳过；后续 1,462 项仅文档 inventory lint 失败，修正后相关回归 339 项通过，无遗留子进程。
- 36 个中央变异与 17 个相关既有变异共 53/53 被捕获；当时全部 2,832 个登记变异的定位片段唯一匹配，未执行整个变异集。
- inventory 示例、临时主机树上的 setup dry-run、当时的消费方补丁检查及差异空白检查通过。真实服务安装与 runner 注册未由这些检查执行。

### 单次运行改造

首轮全量 1,482 项出现 2 个失败和 43 个错误，定位到缺少 Git 元数据及临时目录权限条件。隔离副本复验和后续修正的结果如下：

| 验证 | 结果 |
| --- | --- |
| 主体实现的全量 unittest | 1,483 项，OK，1 项跳过 |
| 重试产物修正后的相关回归 | 310 项通过 |
| 最终队列 job 修正后的中央回归 | 54 项通过 |
| 上述三轮 `tests/leaks.py` | 无遗留子进程 |
| actionlint v1.7.12：三个变更工作流 | 通过 |
| 临时副本将远程模板引用改为本地，检查跨模板输入类型 | 通过 |
| inventory lint、改动空白和文档链接检查 | 通过 |

全量快照早于最后两处修正，最终相关回归覆盖这些修正；不同轮次不能相加当作一次全量测试。

在具备 Git 元数据的隔离副本中复现全量回归；共享主机仍需外层主机锁和资源 scope：

```sh
umask 022
python3 -I tests/leaks.py --timeout 900 -- \
  python3 -B -m unittest discover -s tests -t . -q
```

相关回归和 inventory 检查：

```sh
python3 -I tests/leaks.py --timeout 300 -- \
  python3 -B -m unittest tests.test_central tests.test_central_services \
  tests.test_single_workflow tests.test_reusable tests.test_actions \
  tests.test_hook tests.test_slurmpart tests.test_slurmgate \
  tests.test_part tests.test_no_inventory -q

python3 -I tests/leaks.py --timeout 300 -- \
  python3 -B -m unittest tests.test_central tests.test_central_services \
  tests.test_single_workflow -q

python3 -B -m ci_runners.inventory lint . --deny tests/deny.sha256
```

### 变异检查

累计 61 个中央协议/调度变异和 6 个版本盖章变异被捕获，未修改基线也通过。分批结果为 59/59、7/7、2/2，最后一批重复复验了一个 runner 身份条件。全部 2,857 个登记变异在 harness 版本归一化后唯一匹配，未执行完整变异集。

[test_single_workflow.py](../parallel/tests/test_single_workflow.py) 的 22 项新增测试各有对应变异。完整映射见 [central_mutants.py](../parallel/tests/central_mutants.py) 和 [mutants.py](../tests/mutants.py)。

| 故障注入 | 捕获测试 |
| --- | --- |
| 未申请任务提前占用、重复申请重复分配 | `test_only_requested_tasks_reserve_and_duplicate_requests_are_idempotent` |
| 过早复用 runner、等待整个 run 才释放 | `test_three_tasks_reuse_one_runner_without_waiting_for_the_run_to_end` |
| pytest 失败错误重试或变绿 | `test_assertion_failure_keeps_other_tasks_runnable_and_fails_the_batch` |
| 无限重试或复用旧申请 | `test_retry_requires_a_new_request_and_keeps_the_same_source_run` |
| 错误 job/run attempt/runner 或原事件准入 | `test_another_job_or_source_attempt_cannot_reuse_a_claim`、`GitHubProtocol` 的 claim 和 hook 测试 |
| 缺失 job、未清理或上传失败仍释放/成功 | `test_missing_execution_job_does_not_free_a_reservation_while_source_is_active`、`test_completed_job_waits_for_cleanup_and_upload_failure_cannot_be_green` |
| 无 runner 的排队 job 中断其他任务核对 | `test_queued_job_without_runner_does_not_block_other_task_reconciliation` |
| 队列到期未撤销授权 | `test_queue_deadline_revokes_a_grant_without_cancelling_the_shared_run` |
| verdict 忽略失败或缺失计划 | `test_batch_verdict_fails_on_failure_error_and_cancelled`、`test_missing_plan_is_failure_but_plan_only_validation_needs_no_batch` |
| 重试上传首次结果 | `test_retry_artifact_contains_the_retry_record_and_job_identity` |
| 丢失数据库升级状态 | `test_v1_upgrade_preserves_history_and_refuses_pending_work` |

## 7. 早期现场核对记录（历史快照）

2026-11-19 只读核对发现当前 staging 使用 `ci-staging-*` 临时服务，25 个 listener、0 个 worker。中央数据库版本为 1，无 pending 批次、未释放分配或待发送 outbox；执行端无占用 CPU 组或未发送回报。该快照不是未来停机时的状态保证。

当时 `releases/hook` 和本地版本链接指向开发目录，后续 hook 已可能读取修改后的代码，常驻进程仍持有此前代码。项目配置仍有旧 dispatch 字段，本地消费方仍是旧入口。源码更改不等于新架构已经完成部署。

随后确认本次采用从空运行状态重建：暂停入口、终止所属进程和 Slurm 任务、归档必要记录、清理可再生数据，再安装固定版本并启用新工作流。完整命令和清理边界只保留在 [从头部署手册](v2-single-workflow-run-rebuild.md)。

2026-11-19 按维护确认执行停机和旧身份注销，范围包含 plan runner：

- 停机前再次核对账本：pending 批次、未释放分配、执行端占用和未发送回报均为零；已停止 scheduler、executor 和 25 个 runner，共 27 个 staging 服务。
- 使用仓库 removal token 逐一执行 runner 自带的注销程序。25 次均返回 0 并报告服务端注销成功；所有 `.runner`、`.credentials`、`.credentials_rsaparams` 均已移除。
- 完成时复核 staging 活动服务、listener、worker 和活动任务 scope 均为零。逐项身份、时间和结果原记录于 `ci-runners/state/maintenance/runner-deregistration.json`，现已随历史账本归档到源码目录外，权限为 600，不含令牌。

旧服务凭据访问 runner 管理接口曾返回 HTTP 403，随后通过维护人员提供的 removal token 完成注销。注册身份已经作废，重建必须使用新的 registration token。完成注销不表示新架构已经部署。

同日按维护要求继续清理 `ci_runner_dev`，使其只保留可迁移源码：

- 清理前再次确认无活动 staging 服务、任务 scope、运行目录进程或打开的文件，当前账号的 Slurm 队列为空；中央和执行端无未完成分配或待发送回报。
- 本机配置、历史账本、Slurm 安装记录、注销证据及生成文件先保存到目录外的私密归档，校验 157 个配置和状态文件。含未提交文件的消费方仓库和原始 ZIP 一并移出，保留其中的工作。
- 删除整个旧 `lab/`、工作区日志和已用注销令牌，移除源码中的三个 Python 缓存目录。运行程序、测试环境、缓存、工作目录、数据库、socket、旧 release 链接和本机密钥不再随源码交付。
- 删除完成后核对原有 229 个源码文件的 SHA256 全部一致；随后仅整理忽略规则、README 和两份 v2 文档。首次部署改为从源码外的新数据目录和占位配置开始，不依赖已删除的实验环境。

清理后在主机锁和内存受限 scope 中运行 `tests.test_no_inventory`、`tests.test_central_services.Registration` 及 `tests.test_setup.Setup.test_plan_apply_and_a_second_run`，共 6 个检查通过。覆盖源码 inventory 泄漏检查、schema 2 标签和安装计划、可复用工作流，以及临时主机树上的安装与重复执行；测试使用模拟 systemd，没有安装或启动真实服务。

清理只处理本地开发部署目录；其他部署、共享缓存、系统 Slurm 服务和集群端既有安装及用量历史仍按原边界保留。新设备仍需配置目标资源、重新构建环境并完成现场验收。

待完成的现场验收包括：

- 真实嵌套 job 名称、数字 job ID、runner 关联及 hook 时刻的 API 可见性。
- 多份测试只产生一个 v2 run，SHA 一致；断言失败、上传失败和基础设施重试的最终状态正确。
- 任务数大于容量、并发上限为 1、取消、第二次推送、完整重跑、服务重启及迟到回报不造成重复占用或提前释放。
- 实际 cgroup CPU/聚合内存限制、进程树清理、Slurm 排队与取消、长任务和账户预算。

后续仍需整理配置、安装固定版本、重新注册 runner 并启动服务，组织设置和 release 标签也未由本次操作变更。本地没有 `gh`，GitHub 队列和远程分支状态未在这次现场核对中确认。

接口依据为 GitHub 的 [可复用工作流](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows) 和 [按 run attempt 列出 jobs](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)。这些接口文档不能代替现场验收。

## 8. v2.0.2 发行整理

独立 Git 交付目录保留原开发分支历史及后续本地修复。新增控制层移入 parallel；部署配置、身份、运行账本留在源码外。统一组织注册、角色部署、节点导出和集群维护包。早期清理记录不描述当前服务状态；本次源码重构不迁移原运行服务。后续试部署已完成本地与 Slurm 单工作流 smoke，本次模块迁移与部署脚本仍以本节新验收结果为准。

新增 27 项部署测试覆盖重复执行、部分失败、令牌更新、陈旧计划、外部修改、身份冲突、TLS、导出范围、Slurm 维护包和复制后独立运行。文件修改前先保存执行意图，避免中断后误判为外部修改；旧辅助程序仅复制包文件时仍可导入，完整远端包同时携带 parallel 和 VERSION。

本次在控制节点执行的验收结果：

- 完整 unittest 回归共 1516 项，1515 项通过、1 项实机 GPU P2P 检查按配置跳过，耗时 417.880 秒；进程泄漏检查通过。
- 校验全部 2890 个故障注入片段及测试引用；通过 `tests.mutants.run/one` 在独立副本执行中央调度 66 项、部署 28 项，94/94 被测试拦截。基线为 83 个测试方法；对应表在 [central_mutants.py](../parallel/tests/central_mutants.py) 和 [deployment_mutants.py](../parallel/tests/deployment_mutants.py)。
- inventory lint 为 0 项，使用 123 个拒绝哈希；Git diff 空白检查通过。源码版本、包版本及 50 处 workflow 自引用统一为 2.0.2。
- 临时环境构建 wheel 与源码包；wheel 独立导入报告 2.0.2，源码包的全部 248 个发行文件及可执行位与交付源码摘要一致，解压后的部署入口可独立运行。

完整回归命令如下，`CI_TEST_HOST_LOCK` 使用本机维护的共享锁路径；该变量不进入源码配置：

```bash
umask 022
systemd-run --user --scope -p MemoryMax=4G -p CPUQuota=200% \
  ./bin/ci-lock --lock "$CI_TEST_HOST_LOCK" --timeout 30 -s \
  python3 -I tests/leaks.py --timeout 900 -- \
  python3 -I -m unittest discover -s . -t .
```

安装测试使用临时目录及模拟的 GitHub/systemd/Slurm；TLS 和维护包文件安装在临时目录实际执行。原试部署服务未迁移。交付目录使用独立 `.git`，无共享对象库或外部 worktree 依赖；本次不推送或创建发布标签。组织注册、目标设备资源检查和真实多机断线恢复仍需现场部署验收，不能用这些测试替代。
