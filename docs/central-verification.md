# v2 CPU 集中调度验证记录

分支：`Recycle-the-deployment-of-light`。验证环境为同一工作站上的开发者工作区，系统 Python 3.12.3。长测试使用共享主机锁并放入临时 systemd scope，限制 CPUQuota=100%、MemoryMax=2G；不使用部署账户，不修改其服务或注册。

## 已执行的离线验证

- `python -I tests/leaks.py --timeout 900 -- python -I -m unittest discover -s tests -t . -v`：第一轮运行 1,453 项，结果 OK（1 项跳过），无遗留子进程。覆盖原 schema 1、安装、hook、公开池、GPU 和 Slurm 的既有回归。新增恢复测试后的第二轮运行 1,462 项，仅文档 inventory lint 失败：连续用斜线拼写旧标签被识别为 home 路径；已改为逐个列出标签。
- 最终相关回归：`python -I tests/leaks.py --timeout 300 -- python -B -m unittest tests.test_central tests.test_central_services tests.test_presence tests.test_prefer tests.test_slurmpart tests.test_slurmgate tests.test_part tests.test_no_inventory -q`：339 项全部通过，无遗留子进程，包括上述 lint 修复与最后增加的迟到回报案例。
- `python -m unittest tests.test_central tests.test_central_services -q`：覆盖 schema 2、4 PR × 8 任务、指定设备、累计等待预算、重启、重复事件、取消、组与 runner 分开回收、最终状态及授权执行。资源释放后迟到启动的本地 scope 和 Slurm driver 都有拒绝验证。
- inventory lint 同时扫描已跟踪和新增文件，未发现真实机器清单、账号路径、IP 或凭据。
- `python -m ci_runners.inventory check setup/central/inventory.example.toml`：通过。示例生成 58 个唯一执行 runner，加 `plan-0`、`plan-1`。
- `git apply --check setup/central/brainmass-v2.patch`：在工作区的 brainmass 仓库验证通过，未应用补丁，未修改该仓库的 main。
- `git diff --check`：通过。

安装 dry-run 在临时主机树上调用现有 `setup.build`，没有 apply 或真实 systemctl 操作：新池列出 `ci-executor.service`、唯一 runner 标签及控制 socket；schema 1 的安装计划继续通过原测试。测试为 `Registration.test_executor_setup_plan_is_opt_in_and_lists_the_service`。

## 关键变异测试

`tests/central_mutants.py` 的全部 36 个案例加入现有 `tests/mutants.py`，并复跑本次修改涉及的 17 个既有变异点：最终 53/53 均被对应测试捕获，未修改基线也通过。所有仓库变异的定位片段均检查唯一匹配；未运行整套 2,832 个变异，完整分片仍由仓库 CI 执行。

| 故障注入 | 捕获它的测试 |
|---|---|
| 忽略剩余 CPU 组、破坏 PR 轮转、ACK 再次扣额 | `Scheduling.test_four_prs_atomic_and_fair`、`test_fixed_device_waits_and_cancellation_holds_resources`、`test_reservation_ack_release_and_runner_finish_are_distinct` |
| 执行时长耗尽重试等待预算、旧批次取消新批次 | `Scheduling.test_execution_time_does_not_spend_the_retry_queue_budget`、`test_old_source_does_not_cancel_newer_batch_or_overwrite_its_status` |
| 放过另一主机回报、错误 run/SHA/actor 或资源授权 | `Scheduling.test_host_cannot_ack_another_host_and_timeout_does_not_free`、`GitHubProtocol`、`Admission` |
| 设备/Slurm 准入丢失、固定取 target CPU 计费 | `Admission.test_project_device_and_slurm_admission_cannot_be_bypassed`、`SlurmResources.test_slurm_request_and_budget_use_authorized_cpu_count` |
| 收到成功测试结果便忽略上传失败 | `Scheduling.test_upload_failure_cannot_turn_a_batch_green`、`GitHubProtocol.test_missing_artifact_and_source_cancellation_are_reconciled` |
| 放过错误 CPU 集合/内存上限、截断 workers | `LocalExecution.test_resource_guard_checks_the_full_set_and_aggregate_memory`、`test_workers_are_forwarded_without_clamping_and_ack_follows_limits` |
| scope 仍运行就释放、释放后允许迟到启动 | `LocalExecution.test_ci_work_path_and_cleanup_evidence`、`test_a_delayed_scope_cannot_launch_after_groups_are_released`、`SlurmResources.test_late_slurm_driver_must_check_the_local_allocation` |
| 派发确定未发送后，迟到的设备清理回报堵塞后续报告 | `Scheduling.test_late_executor_tombstone_after_unsent_dispatch_is_acknowledged` |
| 放过未授权 Unix socket 客户端或可公开读取的凭据 | `Rpc.test_socket_authentication_and_replay`、`test_credentials_cannot_be_links_public_files_or_http` |
| 改用 dispatch SHA、漏装执行服务、恢复共享 CPU 标签 | `Registration` |

可以在现有 harness 中选择 `CENTRAL_MUTANTS` 和相关既有案例执行，不需要安装服务；每个案例在独立树中替换一个片段，先验证未修改树通过，再要求指定测试拒绝变异版本。

本次选择的既有变异序号为 `20, 30, 169, 185, 204, 925, 1229, 1230, 1244, 1591, 2131, 2458, 2463, 2464, 2465, 2604, 2624`，在当前树中合并 `CENTRAL_MUTANTS` 后设置 `mutants.WORKERS = 1` 并运行 `mutants.main([])`。这是当前树的序号，后续增加变异时应按修改片段重新核对。

## 尚未执行的部署验收

以下不是离线测试能证明的内容：

1. 每台实际主机的 setup plan、服务账号/socket 权限、TLS、凭据续期及 org 快照更新。
2. 实际 cgroup v1/v2 的 CPU/聚合内存限制，4/12/16 核混合任务及进程树清理。
3. 真实 GitHub 派发、原 SHA checkout、artifact、最终 required status 与取消传播。
4. 集群目标资源、账户预算并发、sbatch 排队与取消、长达 600 分钟的完整运行。
5. GPU/其他旧辅助流程迁移、旧 `light`、`home`、`nightly` 标签退役和实际回退演练。

代码实现和本地版本标记不执行发布。尚未修改任何已部署 release 链接、持久 systemd 服务、runner 注册、组织设置或 GitHub 标签。
