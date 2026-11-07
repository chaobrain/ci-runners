# 使用指南：在 3090 主机上跑仓库的测试

这份指南写给要接入这套 CI 的仓库。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。维护主机的人请看 [operations.md](operations.md)。

## 1. 这是什么，谁能用

- chaobrain 的**每个私有仓库**都能用。runner 在 3090b 和 3090a 两台主机上，每台主机用一个专门的 CI 账户，看不到其他账户的文件和进程（design 决定 12.2、12.16）。h100 已经退出 CI（决定 12.17）。
- **只有 org 成员**触发的任务才会被接受。成员和私有仓库的名单每 10 分钟从 org 自动同步一次（design §6.5），不需要找人加白名单。
- **公开仓库不能用**，继续用 GitHub 托管 runner。org 也不允许 fork 私有仓库，所以 fork 的 PR 跑不到这里。
- 作业都跑在自托管 runner 上，**不消耗**私有仓库的托管 Actions 分钟数。
- 不要把 secret 传给这些任务。

## 2. 管线

```
push / PR（同仓库）/ workflow_dispatch / schedule
        │
        ▼
claim          [self-hosted, <host>-light]
  这个 head 已有结论就跳过（绿的不重跑），否则发 pending
        │
        ▼  （每个作业开跑前，runner 上的 pre-job hook 先检查：
        │    org 成员？快照里的私有仓库，且 id 对得上？快照 ≤ 60 分钟？不是 fork？
        │    不满足就在 "Set up runner" 里拒绝，打印 ci-runners: refused <原因>）
        ▼
部件：每个作业一个 part，跑在某台主机的池子上（<host>-cpu、<host>-gpu、<host>-multi 等）
  CI 槽位 / 租卡 → scope 内运行；内存上限、超时；GPU 健康门和过热提前停止
  结果要求重试时（oom、thermal、unavailable 等）重试一次，跑在 retry-on 给出的标签上
        │
        ▼
salvage        [self-hosted, <host>-light]，verdict 所在主机之外、每台跑过部件的主机一个
        │
        ▼
verdict        [self-hosted, <host>-light]
  汇总各部件最后一次尝试的结果；合并 summary，发 <project>/ci 状态：success / failure / error
```

**部件的结果**（design §4.4）：

| 结果 | 含义 | 之后 |
|---|---|---|
| `pass` | 命令退出码 0 | — |
| `red` | 命令失败或超时，不是基础设施的原因 | 状态为 failure |
| `oom` | 超出内存上限，被杀 | 带 `CI_RESUME=1` 重试一次 |
| `thermal` | 卡过热，被提前停止 | 换一张凉卡重试一次 |
| `reboot` | 运行期间主机重启 | 在同一台主机上重试一次 |
| `unavailable` | 开跑前就不满足条件：卡太热、没有空卡、排队超时、磁盘不足、`gpus` 不合该类 runner 等 | 重试一次 |
| `unavailable`（`drained: …`） | 这台主机或这个池子被维护者排空了（design 决定 13.1） | 在另一台主机的同类池子上重试一次（`retry-on`）；另一台主机没有这一类（`compile`、`nightly`）就不重试，状态为 `error` |
| `unavailable`（`p2p: …`） | 双卡部件（`multi`）开跑前的 P2P 自检失败：主机故障 | 不重试，状态为 `error` |
| `lost` | 作业没留下输出就结束了，例如 runner 被停 | verdict 从主机上的记录 salvage |
| `stopped` | 被取消，例如同一个 PR 推了新 head | — |

只有 `red` 会让部件这一步退出 1。基础设施问题重试一次还没解决，状态就是 `error`，不是 `failure`。

## 3. 接入步骤

1. **写环境规格。** 在默认分支上加 `.github/ci-runners.toml`（见下文）。规格只从默认分支读取，PR 改它不会影响环境。
2. **请维护者建环境。** 维护者在要用的每台主机上写 registry 条目 `etc/projects/<project>.toml`，然后跑 `ci-run env build <project>`，步骤见 [operations.md](operations.md) §4。依赖改动合并之后要重建一次。
3. **加工作流**（见下文），引用 `@v1`。
4. **金丝雀验证。** 在一个分支上分别放一个必然失败的 CPU 测试和一个必然失败的 GPU 测试，确认它们各自只在自己的部件里变红。
5. **完成。** 连续几个 head 端到端跑通，`<project>/ci` 状态和 summary 都对。

**`.github/ci-runners.toml`**（schema 1）：

```toml
schema = 1
python = "3.12"                                   # 环境的 Python 版本（conda）
requirements = ["requirements-dev.txt"]           # pip -r，按顺序；仓库内的路径，-r/-c 引用的文件会一并读取
install = ["pytest-xdist"]                        # 额外的依赖说明符：不能带选项、URL 或路径
[conda]                                           # 可选：pip 装不了的包（例如 NEST）
channels = ["conda-forge"]                        # 按顺序；构建时带 --override-channels
packages = ["nest-simulator=3.10"]                # conda match spec：不能带选项、URL、路径或 channel 前缀
[[upstream]]                                      # 可选：以 editable 方式安装的兄弟仓库
repository = "chaobrain/brainstate"
ref = "origin/main"
[variables]                                       # 写进 registry 条目的环境变量（保留名会被拒绝）
XLA_PYTHON_CLIENT_PREALLOCATE = "false"
```

`conda` 表（design §4.9，决定 12.10）是可选的，不写时与以前完全一样。`packages` 和 `python` 在同一次 `conda create` 里一起求解，之后才跑 pip 的各步（`requirements`、`install`、upstream）。`python` 由 `python` 键决定，`packages` 里不能再写 `python`，同一个包也不能写两次。表里有未知的键、`channels` 里是 URL 或路径、`packages` 里有选项或 URL，整个规格都会被拒绝，什么都不会构建。例如需要 Python 3.13 加 conda-forge 的 `nest-simulator=3.10`，就写 `python = "3.13"` 加上面的 `[conda]` 表。

项目本身**不会**装进环境：部件在作业的 checkout 里运行，`python -m pytest` 会直接从 checkout 导入项目。upstream 在建环境时 clone 并以 editable 方式安装，版本是当时分支的 head。

**让环境跟着 upstream 走**（design §4.9，决定 12.11）。环境会保存建它时用的规格、requirements 文件和每个 upstream 的 commit。维护者在 registry 条目里写 `refresh = true` 后，主机上的 `ci-envrefresh.timer` 每 30 分钟对这个项目运行 `ci-run env refresh`：用 `git ls-remote` 比较每个 upstream 分支的 head 和环境记录的 commit，有分支动了就按保存的规格重建，建好后一次 rename 换上，正在运行的部件继续用它开始时的环境。刷新不带任何令牌，所以**只能用于公开的 upstream**；有私有 upstream 的项目会被拒绝，仍然要维护者用 `env build` 重建。刷新失败时旧环境原样保留，原因写进日志，下一轮再试。默认分支上的规格改了，仍然要维护者手动 `env build`。

**工作流**：仓库在自己的 workflow 里组合 `claim`、`part`、`salvage`、`verdict` 四个 action（design §4.8，决定 12.12），比如先跑一个 plan 作业，再把部件以 matrix 的方式分到几台主机上。规则如下：

1. **每个作业都要加 guard**：`pull_request` 只接受 `head.repo.full_name == github.repository`，此外是 `push`、`workflow_dispatch`、`schedule`。
2. **标签**：写 `self-hosted` 加上某台主机的池子标签，比如 `3090b-cpu`、`3090b-gpu`、`3090b-gpu1`、`3090b-multi`、`3090b-compile`、`3090a-nightly`。不要只写类名。每一类接受的 `gpus`、`lock`、`timeout-minutes` 见 design §4.8 的表，超出范围的部件直接 `unavailable`。`compile` 和 `nightly` 需要仓库在 `policy.toml` 里登记了这一类（design 决定 12.14）。
3. **一个作业只跑一个 `part`**，作业的 `timeout-minutes` 至少比部件的多 15。部件 id 在一次 run 里不能重复。matrix 各分支的输出到不了后面的作业，所以后面的作业要读 `ci-part-<id>-<attempt>` artifact。
4. **重试**：`next` 为 `retry` 或 `fallback` 的部件最多再跑一次，带上 `attempt-of`（第一次的 `record`）和 `resume`，跑在 `retry-on` 给出的标签上，`fallback` 时也可以换到别的主机。在另一台主机上重试时，`attempt-of` 照样填第一次的 `record`（它在第一台主机上，v1.5 起按名字取次数，design 决定 13.3）。
5. **salvage 和 verdict**：verdict 放在最后，跑在某一台跑过部件的主机的 light 池上。它能直接读这台主机上的记录。其他跑过部件的主机，每台在 verdict 之前加一个 `salvage` 作业，跑在那台主机的 light 池上：

   ```yaml
   salvage-3090a:
     needs: [parts]
     if: always()
     runs-on: [self-hosted, 3090a-light]
     steps:
       - uses: chaobrain/ci-runners/actions/salvage@v1
         with:
           parts: cpu gpu multi        # 这次 run 启动的全部部件 id
           wait-seconds: '300'         # 等一个还在跑的本机部件多久
   ```

   对每个 artifact 不全、并且在这台主机上的最新记录属于本次 run 和这次 attempt 的部件，`salvage` 会把记录里的结果作为 `ci-part-<id>-<attempt>` 上传，并带上 `salvaged: true`。不在这台主机上跑的部件不处理；还在跑的部件最多等 `wait-seconds`，超时就是 `lost`。一个 salvage 作业最多上传 16 个部件。
6. **权限**：`contents: read`，claim 和 verdict 还要 `statuses: write`。要读别的 run 的 artifact 时再加 `actions: read`。
7. `concurrency` 必须写成块格式。行内映射 `{group: ...${{ }}...}` 里含表达式时，YAML 会解析失败，GitHub 会直接拒绝整个文件。`tests/test_workflow.py` 会检查本文档里的示例。

各 action 的输入见 `actions/<name>/action.yml`，引用 `@v1`。brainspike 的 `ci.yml` 是一个完整的例子。

## 4. 命令约定

部件的命令在作业的 checkout 里用 `bash -c` 运行。项目环境已经放在 `PATH` 最前面，并设好了 `CONDA_PREFIX`。它应当：

- 只在测试失败时以非零退出；
- 收到 TERM 时停掉自己的进程树（超时会先发 TERM，30 秒后发 KILL）；
- 用 `CUDA_VISIBLE_DEVICES` 给的卡（CPU 部件为空），不自己选卡；进程已经用 taskset 绑到 `CI_CPUS` 列出的 CPU，定并行度时以它为准；
- 把 JUnit XML 写进 `$CI_RESULTS_DIR`，结果会出现在 summary 里；
- 能续跑的话，支持 `CI_RESUME=1`（OOM 之后的重试会带上它）。

命令还能读到这些环境变量：

- `CI_PROJECT`、`CI_PART_DIR`、`CI_RUNNERS_HOST`；
- registry 里的 `variables`；
- runner 的 `https_proxy`、`http_proxy`、`no_proxy`。主机访问外网要经过 egress，所以 pip、git、下载数据集都会自动走代理；只能访问 egress 允许清单上的域名（见 [operations.md](operations.md) §6）。

内存上限来自 registry 的 `memory`。超出就是 `oom`，不算 `red`。

## 5. 看结果与常见问题

- **`<project>/ci` 状态**就是结论。verdict 作业的 summary 里列出每个部件的结果、主机、卡、排队和运行时间、峰值温度。
- **"Set up runner" 里出现 `ci-runners: refused <原因>`**：被 hook 拒绝了。常见原因：

  | 原因 | 意思 |
  |---|---|
  | `actor` | 触发者不是 org 成员（外部协作者、bot 等） |
  | `bot` | bot 触发的任务，而这个仓库没有开 `bot_dispatch` |
  | `repository` / `repository-id` | 仓库不在快照里（公开仓库，或刚建、还没同步），或者 id 对不上 |
  | `fork` | 来自 fork 的 PR |
  | `event` | 这个事件不被接受，例如 `pull_request_target` |
  | `paused` | 维护者暂停了这个仓库 |
  | `stale` | org 快照超过 60 分钟没更新：同步出了问题，请联系维护者 |
  | `runner-class` | 这个 runner 不接这一类任务；`compile` 和 `nightly` 只接 `policy.toml` 里列了这一类的仓库 |
  | `policy` / `timeout` / `error` | hook 无法做出判断，请联系维护者 |

- **新建的私有仓库或新成员**：最多等 10 分钟，下一次同步后就能用。
- **想对同一个 head 重跑**：已经绿的 head 会被 claim 跳过，要重跑就推一个新 commit。红的 head 或 error 的 head 重新触发会再跑一次。
- **`unavailable: health gate`**：分到的卡太热，会自动换一张凉卡重试。
- **作业排队很久**：CPU 部件共用主机的 CI 槽位，GPU 部件一张卡一次只跑一个。每个项目在每个池子里最多有 1 个等待中的部件，再多就直接 `unavailable`（"project queue full"）。

## 6. 主机锁

`part` 的 `lock` 输入可以是 `s`、`x`、`l` 或 `none`，表示整条命令执行期间持有主机锁（brainspike 的 `host_lock.sh` 协议，design 决定 12.9）。`compile` 默认 `x`，其他类默认 `none`。`none` 时命令按需自己加锁，比如 brainspike 的 `local_ci.sh` 每一步单独加锁。持锁时，命令能读到 `CI_HOST_LOCK` 和 `LOCAL_CI_HOST_LOCK`（锁文件路径）以及 `BRAINSPIKE_HOST_LOCK`（所持的模式）。命令里再调用 `host_lock.sh`，已持有的锁能覆盖的请求就不再等待：在 `x` 下的任何请求，或在 `s` 下的 `-s`。在 `s` 下请求 `-x` 会失败（状态 3），因为它要等的正是自己。部件先拿槽位和卡，最后才拿锁，拿锁的等待也计入 `queue-timeout-minutes`，超时就是 `unavailable`。
