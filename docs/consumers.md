# 使用指南：在 3090 主机上跑仓库的测试

这份指南写给要接入这套 CI 的仓库。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。维护主机的人请看 [operations.md](operations.md)。

## 1. 这是什么，谁能用

- chaobrain 的**每个私有仓库**都能用。runner 在 3090b、3090a 和 h100 三台主机上：3090 主机各用一个专门的 CI 账户，看不到其他账户的文件和进程（design 决定 12.2、12.16）；h100 回到了 CI，用的是共享账户（决定 15.8）。
- **只有 org 成员**触发的任务才会被接受。成员和私有仓库的名单每 10 分钟从 org 自动同步一次（design §6.5），不需要找人加白名单。
- **公开仓库不能用**，继续用 GitHub 托管 runner。org 也不允许 fork 私有仓库，所以 fork 的 PR 跑不到这里。
- 作业都跑在自托管 runner 上，**不消耗**私有仓库的托管 Actions 分钟数。
- 不要把 secret 传给这些任务。
- **即将变化（design 决定 16，按 RUN-10 分阶段上线）**：工作流只写能力标签（`ci-cpu`、`ci-gpu`、`ci-gpu2`、`ci-timing` 等），不再写主机标签；ci-runners 提供可复用工作流 `ci.yml`，仓库只需给一个打印计划 JSON 的 `plan` 命令，接入不超过 20 行工作流加一个 registry 条目（design §4.10）；选择加入的公开仓库可以用单独的公开池（design 决定 16.7），fork 的 PR 仍然在第一步之前被拒。各阶段上线之前，下面写的仍是现在的用法，主机标签照常可用。

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
| `unavailable`（`busy: …`） | 第一次尝试时这台主机被其他账户占满了：原因写出规则、测到的值和阈值（design 决定 13.2） | 同上，在另一台主机的同类池子上重试一次；同一个 head 的同一个部件只让出一次 |
| `unavailable`（`busy: nightly holds every slot and card`） | 第一次尝试时这台主机的 `nightly` 部件占着（或正排队等着）全部槽位和卡（design 决定 13.6），不再白等排队超时 | 立刻在另一台主机的同类池子上重试一次；每次都让出 |
| `unavailable`（`p2p: …`） | 双卡部件（`multi`）开跑前的 P2P 自检失败：主机故障 | 不重试，状态为 `error` |
| `lost` | 作业没留下输出就结束了，例如 runner 被停 | verdict 从主机上的记录 salvage |
| `stopped` | 被取消：同一个 PR 推了新 head、有人取消了运行，或者 GitHub 单独取消了这个作业（runner 和 GitHub 断了连接、作业超过 `timeout-minutes`） | 重试一次（同一个池子）；整个运行被取消时 retry 作业带 `!cancelled()` 不会运行，所以只有运行还在继续的取消才重试（design 决定 15.4） |

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
# contract = 1                                    # 可选（决定 16.6）：一个整数，从 R1 起接受、不起作用，见下文
[conda]                                           # 可选：pip 装不了的包（例如 NEST）
channels = ["conda-forge"]                        # 按顺序；构建时带 --override-channels
packages = ["nest-simulator=3.10"]                # conda match spec：不能带选项、URL、路径或 channel 前缀
[[upstream]]                                      # 可选：以 editable 方式安装的兄弟仓库
repository = "chaobrain/brainstate"
ref = "origin/main"
[variables]                                       # 写进 registry 条目的环境变量（保留名会被拒绝）
XLA_PYTHON_CLIENT_PREALLOCATE = "false"
[variant.cuda12]                                  # 可选：集群目标的环境变体（只用于 slurm 类）
install = ["jax[cuda12]"]                         # 替换基础的 install
conda = { packages = ["cuda-nvcc=12.2"] }         # 可选：加到基础的 conda 包里，同一次求解
```

`conda` 表（design §4.9，决定 12.10）是可选的，不写时与以前完全一样。`packages` 和 `python` 在同一次 `conda create` 里一起求解，之后才跑 pip 的各步（`requirements`、`install`、upstream）。`python` 由 `python` 键决定，`packages` 里不能再写 `python`，同一个包也不能写两次。表里有未知的键、`channels` 里是 URL 或路径、`packages` 里有选项或 URL，整个规格都会被拒绝，什么都不会构建。例如需要 Python 3.13 加 conda-forge 的 `nest-simulator=3.10`，就写 `python = "3.13"` 加上面的 `[conda]` 表。

`contract`（design §4.9，决定 16.6）是一个非负整数：可复用工作流上线后，plan 作业会拿 head 的值和默认分支的比，比默认分支小或缺失的 head 直接红，提示 merge origin/main。在那之前它只被接受、不起作用（R1，决定 16.3）；`v1` 指向接受它的版本之前不要写，否则更旧的主机层会拒绝整个规格。

`variant` 表（design §4.9，决定 14.9）只给 `slurm` 类用：集群站点配置里每个目标有一个环境变体（例如 `cpu`、`cuda12`、`cuda13`），`ci-run slurm env` 为规格里写了的每个变体单独建一个环境，用它的 `install` 替换基础的 `install`（`requirements`、upstream 都不变）；规格里没写的变体用基础环境。变体还可以有一个 `conda` 表（`packages`，需要时加 `channels`，检查规则和基础的一样）：它的包加在基础的 `conda` 包后面，在同一次 `conda create` 里一起求解；它的 channel（默认没有）排在基础的后面。变体不能再写基础已经写了的包或 channel。表里只能有 `install` 和 `conda`，有其他键整个规格都会被拒绝。3090 主机上的 `ci-run env build` 只读基础部分，有没有这个表结果都一样。所以要按变体换的包（例如 jax 的 CUDA 插件、和变体 CUDA 版本一致的 nvcc）放在变体里，不要放进 `requirements` 文件或基础部分。

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
6. **权限**：`contents: read`，claim 和 verdict 还要 `statuses: write`，verdict 另要 `actions: read`：它列出本 run 的 artifact，每个部件只读最新一次 run attempt 的，这样“Re-run failed jobs”能让 run 变绿（issue #169）；没有这个权限时照旧读下载到的 artifact。要读别的 run 的 artifact 时再加 `actions: read`。
7. `concurrency` 必须写成块格式。行内映射 `{group: ...${{ }}...}` 里含表达式时，YAML 会解析失败，GitHub 会直接拒绝整个文件。`tests/test_workflow.py` 会检查本文档里的示例。

各 action 的输入见 `actions/<name>/action.yml`，引用 `@v1`。不要钉某个发布 tag（如 `@v1.4.1`）：主机升级后，inventory 里新加的键（v1.5 的 `peer_labels`、`busy`）会让更旧的 action 在那台主机上全部失败（design §9，`v1.5.0` notes 第 6 条）。brainspike 的 `ci.yml` 是一个完整的例子。

## 4. 命令约定

部件的命令在作业的 checkout 里用 `bash -c` 运行。项目环境已经放在 `PATH` 最前面，并设好了 `CONDA_PREFIX`。它应当：

- 只在测试失败时以非零退出；
- 收到 TERM 时停掉自己的进程树（超时会先发 TERM，30 秒后发 KILL）；
- 用 `CUDA_VISIBLE_DEVICES` 给的卡（CPU 部件为空），不自己选卡；进程已经用 taskset 绑到 `CI_CPUS` 列出的 CPU（cpuset 列表，例如 `0-3,8-9`，不是个数；集群部件是作业在节点上分到的 CPU），定并行度时以它为准；
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
- **想对同一个 head 重跑**：已经绿的 head 会被 claim 跳过，要重跑就推一个新 commit。红的 head 或 error 的 head 重新触发会再跑一次。claim 只认 runner 自己（workflow bot）发的状态：个人发的 `pending` 或 `success`（比如旧式的本地运行）既不会占住 head，也不会让它被跳过（design 决定 15.9）。
- **`unavailable: health gate`**：分到的卡太热，会自动换一张凉卡重试。
- **作业排队很久**：CPU 部件共用主机的 CI 槽位，GPU 部件一张卡一次只跑一个。每个项目在每个池子里最多有 1 个等待中的部件，再多的第一次尝试就直接 `unavailable`（"project queue full"），去另一台主机重试；重试本身不受这个上限限制，会排队等到排队超时（design 决定 13.6）。

## 6. 主机锁

`part` 的 `lock` 输入可以是 `s`、`x`、`l` 或 `none`，表示整条命令执行期间持有主机锁（brainspike 的 `host_lock.sh` 协议，design 决定 12.9）。`compile` 默认 `x`，其他类默认 `none`。`none` 时命令按需自己加锁，比如 brainspike 的 `local_ci.sh` 每一步单独加锁。持锁时，命令能读到 `CI_HOST_LOCK` 和 `LOCAL_CI_HOST_LOCK`（锁文件路径）以及 `BRAINSPIKE_HOST_LOCK`（所持的模式）。命令里再调用 `host_lock.sh`，已持有的锁能覆盖的请求就不再等待：在 `x` 下的任何请求，或在 `s` 下的 `-s`。在 `s` 下请求 `-x` 会失败（状态 3），因为它要等的正是自己。部件先拿槽位和卡，最后才拿锁，拿锁的等待也计入 `queue-timeout-minutes`，超时就是 `unavailable`。

## 7. Slurm 集群（design 决定 14，RUN-9 起）

按计费时长收费的 Slurm 集群作为 3090 主机 runner 的**远程目标**：`slurm` 类的部件在 3090 主机上运行，不占这台主机的 CI 槽位和卡，把 checkout 同步到集群的共享文件系统，以一个 `sbatch` 作业运行命令，日志实时转回作业日志，结束后把结果同步回来（design 决定 14.3）。集群的主机名、分区、节点、账户和路径都只写在维护者主机上的私有站点配置里，不进仓库。

- **按仓库开通**：和 `compile`、`nightly` 一样，`slurm` 类只接 `policy.toml` 里列了它的仓库（决定 14.2）。
- **标签和输入**：作业用 `[self-hosted, <host>-slurm]` 或共享标签；`part` 多一个可选输入 `target`，写站点配置里的目标名，或者按尝试顺序写几个，用逗号或空格隔开（例如按卡价从低到高：`a40,h100`）；主机上有多个站点配置时写成 `<site>:<target>`。`slurm` 类必须给 `target`。`gpus` 最多为该目标的 `max_cards`，`lock` 只能是 `none`，`timeout-minutes` 不超过该目标的 `max_minutes`。
- **计费**：只有作业运行期间计费，排队不计费。排队超过每个目标的上限就取消、换下一个账户或目标；超过 `queue-timeout-minutes` 就 `unavailable`（`pending`）。会超出每日或每月预算的部件直接 `unavailable`（`budget`）（决定 14.7）。summary 里写出每个部件的目标和花费。
- **命令约定**同 §4，另有几点：节点不限制内存，部件的内存上限由集群上的 supervisor 按 `RssAnon` 执行，超出就是 `oom`；计算节点**不能访问外网**，命令不能下载任何东西；`RUNNER_TEMP` 和 `CI_PART_DIR` 指向集群上的目录，结束后同步回 runner，所以之后的 upload 步骤不用改；双卡部件开跑前先做 P2P 自检。
- **结果**（design §4.4，没有新结果）：节点故障是 `unavailable`（`node fail`），在同一目标上重试一次；被站点其他用户取消、排队超时、预算不够、目标尚未启用是 `unavailable`，换池子；P2P 自检失败是 `unavailable`（`p2p`），不重试；登录节点 5 分钟连不上是 `lost`，verdict 从 3090 主机上的记录 salvage。
- **目标先过验收再计入**（决定 14.10）：只读检查、canary、一个真实的单卡部件、一个带 P2P 自检的真实双卡部件，全部通过后维护者才在站点配置里启用它；未启用的目标会被拒绝（`unavailable`）。
- **安全**：部件以集群账户的身份运行，能读到这个账户在集群上的所有文件（design T19）。所以只有私有仓库、只有 org 成员，并且要用专门的 CI 账户，或者账户主人自己的账户，但要先把这个账户上给其他工具用的凭据移走（决定 14.12）；不要把 secret 传给这些任务。
- **公开的内容**：部件在 GitHub 上显示的作业日志、原因和摘要里，集群的根目录、账户、用户、登录节点、分区和节点名都换成了 `<root>`、`<node>` 这样的占位符（design 14.4）。但部件从集群取回的文件（`CI_PART_DIR` 里的结果、`RUNNER_TEMP` 里的文件：测试结果、coverage 数据、命令自己写的日志）**不替换**，里面可能有集群上的绝对路径和节点名。把它们上传成 artifact、写进步骤摘要或 PR 正文，就等于公开这些名字；仓库是私有的时候只有 org 成员能看到。要公开就先处理：例如 coverage 用 `relative_files = true`，摘要里写 `CI_RUNNERS_HOST` 而不是节点的主机名。
