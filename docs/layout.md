# 源码结构与模块职责

仓库按职责组织 Python 实现、部署脚本和测试。一次任务的主要调用链是：

```text
官方 Actions Runner
  ├─ hooks/runner/ → security/hook.py → security/policy.py
  └─ actions/<action>/action.yml → actions/lib/ci-run.sh
       → bin/ci-run → ci_runners/cli.py
         ├─ workflow/：claim、verdict、salvage
         ├─ execution/ → resources/：主机任务执行
         ├─ pools/：资源探测、跨机执行和主机验收
         └─ slurm/：驱动主机 → SSH Gate → 集群作业
```

## Python 实现

```text
ci_runners/
├── __init__.py                       # 包版本
├── cli.py                            # ci-run 子命令分派
├── layout.py                         # 统一定位 checkout / release 根目录
├── config/
│   ├── inventory.py                  # 主机配置加载、校验和敏感信息审计
│   └── registry.py                   # 项目注册表、环境、资源与重试默认值
├── workflow/
│   ├── claim.py                      # 纯判定：提交是否需要执行
│   ├── github.py                     # GitHub REST 客户端与 claim 命令
│   ├── outcome.py                    # 结果分类及 retry/fallback 提示
│   └── verdict.py                    # 最终汇总、salvage、状态和 summary
├── execution/
│   ├── part.py                       # 任务编排、supervisor、记录和资源生命周期
│   └── sampler.py                    # 运行中的 GPU 采样与过热停止
├── resources/
│   ├── locks.py                      # 主机共享/独占/light 锁
│   ├── slots.py                      # 并发槽、公平队列、项目配额
│   ├── gpus.py                       # GPU 解析、选卡与 lease 授权规则
│   └── p2p.py                        # CUDA 多卡实际拷贝自检
├── pools/
│   ├── pool.py                       # 机器注册表、容量探测、选机、打包和执行
│   └── pooladd.py                    # 主机接入、验收、预热和 ready 状态
├── slurm/
│   ├── slurmsite.py                  # 站点、目标、账户、价格及脱敏
│   ├── slurmpart.py                  # 驱动端选择、提交、轮询、日志、取消与回收
│   ├── slurmgate.py                  # 集群受限网关、batch 模板和作业启动
│   ├── slurmsetup.py                 # check/plan/apply/status/cost 等运维编排
│   ├── slurmlogin.py                 # 登录节点部署和构建实现，经 SSH 发送源码运行
│   ├── slurmenv.py                   # 远端环境与 variant 构建编排
│   └── slurmcanary.py                # 真实短作业验收
├── environments/
│   ├── envbuild.py                   # 从默认分支规格构建、刷新并清理环境
│   └── envuse.py                     # 保护运行中任务正在使用的环境构建
├── security/
│   ├── policy.py                     # 事件、仓库、actor、fork 和 runner 类别准入
│   ├── hook.py                       # 作业开始准入与完成收尾
│   ├── hookcopy.py                   # 私有 /tmp 中的 hook 副本维护
│   └── orgsync.py                    # 组织成员与私有仓库快照同步
├── deployment/
│   └── setup.py                      # 主机计划、应用、模板渲染及 Runner 安装
├── network/
│   └── bridge.py                     # 域名白名单 HTTPS CONNECT 出口代理
└── operations/
    ├── drain.py                      # 主机或池暂停/恢复接单
    ├── hostload.py                   # 外部负载采样与避忙判断
    ├── reaper.py                     # 过期日志和执行记录清理
    └── runnerwatch.py                # GitHub Runner 在线监测和 issue 告警
```

各子目录都有 `__init__.py`。这次调整保留实现文件名和原有职责；例如
`workflow/github.py` 仍同时承载 HTTP 客户端与 claim 命令，后续可另行拆分。

## Action 与命令入口

| 目录或文件 | 作用 |
|---|---|
| `actions/{claim,part,salvage,verdict}/action.yml` | 消费仓库的公开 composite Action |
| `actions/lib/ci-run.sh`、`actions/RELEASE` | 选择主机上已安装的运行时代码版本 |
| `bin/ci-run` | 统一业务及运维 CLI |
| `bin/ci-lock`、`bin/ci-slot` | 持有主机锁或并发槽执行命令 |
| `bin/ci-slurm-gate` | 集群 SSH forced command 入口 |
| `hooks/runner/job-started.sh`、`job-completed.sh` | 官方 Runner 生命周期钩子的源码模板 |

公开 Action 引用仍为 `chaobrain/ci-runners/actions/<name>@v1`。

## 部署与维护脚本

```text
setup/
├── host/
│   ├── install.sh                    # 安装指定 release，并切换 releases/hook
│   ├── plan.sh                       # 打印主机部署计划
│   ├── apply.sh                      # 应用已确认计划
│   ├── register.sh                   # 注册并启动官方 Actions Runner
│   ├── uninstall.sh                  # 撤销本系统管理的主机服务
│   └── units/                        # CI 主机 user systemd 服务、timer、slice 模板
├── config/
│   ├── inventory.example.toml        # 主机配置占位示例
│   └── policy.org.example.toml       # 组织准入策略占位示例
├── egress/
│   ├── allow                         # 出口域名白名单
│   └── ci-egress.service             # 出口主机系统服务
├── slurm/
│   └── site.example.toml             # 集群站点配置占位示例
└── maintenance/
    ├── orgsync/                      # orgsync-push.sh 及配套 service/timer
    ├── nightly/                      # nightly-backstop.sh 及配套 service/timer
    └── runnerwatch/                  # runner-watch.sh 及配套 service/timer
```

`host/units/` 内包含 Runner、hook、tunnel、reaper、hostload、orgsync、
envrefresh 服务及 CI/项目资源 slice。`maintenance/` 的任务由维护者账户运行；
其中 nightly backstop 集成了 brainspike 的 `scripts/merge_gate.py`。

源码模板的位置与实际安装位置分别维护。主机仍使用 `releases/`、`etc/`、
`state/`、`envs/`、`hooks/`，实际 hook 文件仍直接安装在主机的 `hooks/` 下。
服务名、Runner 标签、配置 schema 和 Action 输入输出保持原契约。

## 测试与开发命令

`tests/` 中的测试按 `interfaces`、`workflow`、`security`、`execution`、
`resources`、`pools`、`slurm`、`environments`、`config`、`deployment`、
`network`、`operations`、`support` 分类；fixtures、测试替身、变异与进程泄漏
检查脚本保留在测试根目录，以便所有类别共用。

在仓库根目录运行：

```sh
python -I tests/leaks.py --timeout 900 -- python -I -m unittest discover -s tests -t . -v
python -m ci_runners.config.inventory lint . --deny tests/deny.sha256
python tests/mutants.py
```

Python 运维模块入口随目录调整为：

```text
python -m ci_runners.config.inventory ...
python -m ci_runners.slurm.slurmsite ...
python -m ci_runners.pools.pool ...
```

`bin/ci-run`、`bin/ci-lock`、`bin/ci-slot`、`bin/ci-slurm-gate` 的命令名和参数
保持不变。打包配置与远端 source bundle 均递归包含 Python 子包；新增组件时
应同时检查隔离子进程的模块路径及远端运行所需的源码是否齐全。
