# 使用指南：在 3090 主机上跑仓库的测试

这份指南写给要接入这套 CI 的仓库。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。维护主机的人请看 [operations.md](operations.md)。

## 1. 这是什么，谁能用

- chaobrain 的**每个私有仓库**都能用。runner 在 3090b、3090a 和 h100 三台主机上：3090 主机各用一个专门的 CI 账户，看不到其他账户的文件和进程（design 决定 12.2、12.16）；h100 回到了 CI，用的是共享账户（决定 15.8）。
- **只有 org 成员**触发的任务才会被接受。成员和私有仓库的名单每 10 分钟从 org 自动同步一次（design §6.5），不需要找人加白名单。
- **公开仓库不能用**，继续用 GitHub 托管 runner。org 也不允许 fork 私有仓库，所以 fork 的 PR 跑不到这里。
- 作业都跑在自托管 runner 上，**不消耗**私有仓库的托管 Actions 分钟数。
- 不要把 secret 传给这些任务。
- **即将变化（design 决定 16，按 RUN-10 分阶段上线）**：工作流只写能力标签（`ci-cpu`、`ci-gpu`、`ci-gpu2`、`ci-timing` 等），不再写主机标签；ci-runners 提供可复用工作流 `ci.yml`，仓库只需给一个打印计划 JSON 的 `plan` 命令，接入不超过 20 行工作流加一个 registry 条目（design §4.10）；选择加入的公开仓库只有 GPU 作业用公开池，每个作业在一个隔离的单次 runner 里运行，CPU 作业仍在 GitHub 托管 runner 上（design 决定 16.7、18），fork 的 PR 仍然在第一步之前被拒。各阶段上线之前，下面写的仍是现在的用法，主机标签照常可用。

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

`contract`（design §4.9，决定 16.6）是一个非负整数：调用可复用工作流 `ci.yml` 时，plan 作业会拿 head 的值和默认分支的比，比默认分支小或缺失的 head 直接红，提示 merge origin/main（§8）。自己组合 action 的工作流不读它。`v1` 指向接受它的版本（R1 起，决定 16.3）之前不要写，否则更旧的主机层会拒绝整个规格。

`variant` 表（design §4.9，决定 14.9）只给 `slurm` 类用：集群站点配置里每个目标有一个环境变体（例如 `cpu`、`cuda12`、`cuda13`），`ci-run slurm env` 为规格里写了的每个变体单独建一个环境，用它的 `install` 替换基础的 `install`（`requirements`、upstream 都不变）；规格里没写的变体用基础环境。变体还可以有一个 `conda` 表（`packages`，需要时加 `channels`，检查规则和基础的一样）：它的包加在基础的 `conda` 包后面，在同一次 `conda create` 里一起求解；它的 channel（默认没有）排在基础的后面。变体不能再写基础已经写了的包或 channel。表里只能有 `install` 和 `conda`，有其他键整个规格都会被拒绝。3090 主机上的 `ci-run env build` 只读基础部分，有没有这个表结果都一样。所以要按变体换的包（例如 jax 的 CUDA 插件、和变体 CUDA 版本一致的 nvcc）放在变体里，不要放进 `requirements` 文件或基础部分。

项目本身**不会**装进环境：部件在作业的 checkout 里运行，`python -m pytest` 会直接从 checkout 导入项目。upstream 在建环境时 clone 并以 editable 方式安装，版本是当时分支的 head。

**让环境跟着 upstream 走**（design §4.9，决定 12.11）。环境会保存建它时用的规格、requirements 文件和每个 upstream 的 commit。维护者在 registry 条目里写 `refresh = true` 后，主机上的 `ci-envrefresh.timer` 每 30 分钟对这个项目运行 `ci-run env refresh`：用 `git ls-remote` 比较每个 upstream 分支的 head 和环境记录的 commit，有分支动了就按保存的规格重建，建好后一次 rename 换上，正在运行的部件继续用它开始时的环境。刷新不带任何令牌，所以**只能用于公开的 upstream**；有私有 upstream 的项目会被拒绝，仍然要维护者用 `env build` 重建。刷新失败时旧环境原样保留，原因写进日志，下一轮再试。默认分支上的规格改了，仍然要维护者手动 `env build`。

**工作流**：仓库在自己的 workflow 里组合 `claim`、`part`、`salvage`、`verdict` 四个 action（design §4.8，决定 12.12），比如先跑一个 plan 作业，再把部件以 matrix 的方式分到几台主机上。规则如下：

1. **每个作业都要加 guard**：`pull_request` 只接受 `head.repo.full_name == github.repository`，此外是 `push`、`workflow_dispatch`、`schedule`。
2. **标签**：写 `self-hosted` 加上某台主机的池子标签，比如 `3090b-cpu`、`3090b-gpu`、`3090b-gpu1`、`3090b-multi`、`3090b-compile`、`3090a-nightly`。不要只写类名。每一类接受的 `gpus`、`lock`、`timeout-minutes` 见 design §4.8 的表，超出范围的部件直接 `unavailable`。`compile` 和 `nightly` 需要仓库在 `policy.toml` 里登记了这一类（design 决定 12.14）。
3. **一个作业只跑一个 `part`**，作业的 `timeout-minutes` 至少比部件的多 15；部件可能落到有槽位的 runner 上时，按部件的分钟数 × 标签里最大的 `speed` 再加 15（design 16.2，内层超时会按 `speed` 放大，作业的上限要盖住它）。部件 id 在一次 run 里不能重复。matrix 各分支的输出到不了后面的作业，所以后面的作业要读 `ci-part-<id>-<attempt>` artifact。
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
- 在有槽位的 runner 上（design 16.2，从 RUN-10 的 R2 起；见下面的“槽位”）：`CI_WORKERS`、`CI_SPEED`，以及槽位设了 `isa` 时的 `CI_ISA`；
- registry 里的 `variables`；
- runner 的 `https_proxy`、`http_proxy`、`no_proxy`。主机访问外网要经过 egress，所以 pip、git、下载数据集都会自动走代理；只能访问 egress 允许清单上的域名（见 [operations.md](operations.md) §6）。

内存上限来自 registry 的 `memory`。超出就是 `oom`，不算 `red`。

**槽位**（design 16.2，从 RUN-10 的 R2 起）。一个部件 runner 就是一个槽位：固定的一组 CPU 和一个内存上限。部件跑在这样的 runner 上时：

- 进程绑到槽位的 cpuset，`CI_CPUS` 就是它；
- 内存上限是槽位的 `memory`；部件自己的 `memory` 更小时用它，但永远不超过 registry 的上限；
- 内层超时是部件的 `timeout-minutes` × 槽位的 `speed`（参考主机上是 1.0）：慢的主机按比例多给时间，`timeout-minutes` 照旧写在参考主机上需要的分钟数；
- 命令多拿到三个变量，作业自己设的同名变量不会传进来：
  - `CI_WORKERS`：该起几个 worker 进程，例如 `pytest -n "${CI_WORKERS:-auto}"`。慢的主机给的 worker 更多；不要自己按 CPU 个数另算。
  - `CI_SPEED`：这个槽位上一个测试比参考主机慢几倍。命令自己的单个测试超时乘上它，例如 `pytest --timeout "$(awk -v s="${CI_SPEED:-1}" 'BEGIN { print int(60 * s + 0.5) }')"`；计时类断言也一样。
  - `CI_ISA`（只在槽位设了 `isa` 时有）：命令可以用的最高指令集级别，例如 `x86-64-v3`（AVX2）。执行器不强制它：命令要把它传给自己的数值库（比如 XLA 的 `--xla_cpu_max_isa`、oneDNN 的 `ONEDNN_MAX_CPU_ISA`），这样同一个标签下各主机的结果才能逐位相同。

没有槽位的 runner 不设这三个变量，其它一切照旧，所以命令要给它们默认值（`${CI_WORKERS:-…}`、`CI_SPEED` 缺省按 1）。

**计划里的 `needs`**（design 决定 16.5 和 §4.10，RUN-10 R5 起；可复用工作流 R6 上线后生效）。计划里每个部件写它要什么，不写主机：

| `needs` | 标签 | 用途 |
|---|---|---|
| `cpu` | `ci-cpu` | 一个 CPU slot 的部件 |
| `gpu` | `ci-gpu` | 一张卡 |
| `gpu2` | `ci-gpu2` | 两张卡，通过 P2P 自检的 |
| `timing` | `ci-timing` | 独占的计时部件（`compile` 类），从不溢出到集群 |

- `minutes` 是这个部件在参考主机上的时长（不超过 240）。作业的 `timeout-minutes` 是 `minutes` 乘这组标签里最慢的 slot 的 `speed`，再加 15；部件自己的超时按它拿到的那个 slot 的 `speed` 放大。
- `memory_gib` 可选，不写就用 registry 的默认值。超过这个标签不带 `mem-big` 的最大 slot 时，路由自动加 `mem-big`；连 `mem-big` 的 slot 都放不下，计划直接失败。
- 部件只在所有主机都没有空闲 slot、而集群现在就能起它时才去 `ci-slurm`；集群起不了就留在共享标签上排队。没有任何主机提供的标签（带它的主机都掉线或 drained）在计划时就报错，不会排队等 24 小时。
- 不认识的 `needs`、多余的键、重复的 id 都会让 plan 作业失败并给出原因。
- **优先主机**（design 决定 19，R5b 起；运维给主机定了 `tier`、打开 presence 后生效）：所有测试先在 h100 上跑。计划里不写主机，部件只带能力标签；后面层级的主机在 h100 还有空闲 runner 时把自己的空闲 runner 停着，所以作业先到 h100；h100 满了，其他主机的 runner 大约一分钟内上线接剩下的部件。计划不能指定或改变这个顺序。故意写某台主机自己标签的作业（例如 nightly 要在参考主机上测秒数）要先在那台主机的 `home` 标签上跑 `ci-run presence hold --minutes N`，结束时 `--release`；仓库在那台主机的 policy 条目里要有 `nightly` 类（design 19.8）。一个 run 的作业共用一个保持：只在 run 的最后一个作业里 `--release`。
- **主机的 `home` 标签从哪里来**（design §5.1，2026-11-13 随 issue #232 定）：每台主机除了 light 池，还可以有一个 home 池：一个 runner（`<role>-home-1`），不带 `ci-` 标签，presence 从不停它（drain 和自动 drain 时也不停），只有从它上面的作业能 `presence hold` 这台主机。它的标签是 `<role>-home`（只有它有）和 `<role>-light`（和 light 池共用，salvage 用）。不要自己拼标签，从 `ci-run pool status --json` 读：在某台主机的那一行（`role` 是它的角色）的 `pools` 里，找 `class` 是 `light`、`labels` 里没有 `ci-` 开头标签的那一项；它的标签里，这一行其他池都不带的那一个，就是这台主机的 home 标签。找不到（这一行没有 `pools`：主机的 release 太旧，或者这一行来自维护者自己登录发出的探测；或者没有这样的池、或者不止一个）就说明这台主机没有 home runner，不能在那里保持，作业不要写它的主机标签。例子（只列出用到的字段；这里 home 标签是 `3090b-home`）：

  ```json
  [{"key": "3090b", "role": "3090b", "problems": [],
    "pools": [{"pool": "light", "class": "light", "labels": ["ci-light", "3090b-light"], "registered": 6, "online": 6, "free": 5},
              {"pool": "home", "class": "light", "labels": ["3090b-home", "3090b-light"], "registered": 1, "online": 1, "free": 1},
              {"pool": "part", "class": "part", "labels": ["ci-cpu", "3090b-cpu"], "registered": 3, "online": 3, "free": 3}]}]
  ```

  home runner 不带 `ci-light`，所以 `ci-light` 的空闲数（路由读的）不算它。

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

## 8. 计划 JSON 与 `contract`（design §4.10，决定 16.6，RUN-10 R6 起）

可复用工作流 `ci.yml` 的 plan 作业在 `ci-light` 上、head 的 checkout 和项目环境里运行仓库的 `plan` 命令（它是 plan 作业里一个叫 `plan` 的部件，内层上限 30 分钟）。命令的**标准输出**必须正好是一个 JSON 文档（最多 1 MiB）；日志请写到标准错误。没有 `plan` 命令时，计划就是 `{"schema": 1, "parts": [{"id": "pytest", "command": "python -m pytest", "needs": "cpu", "minutes": 60}]}`。

**计划 JSON，schema 1**（`ci-run flow plan` 检查，`ci_runners/flow.py`；键名、类型和报错都是精确的）：

| 键 | 类型 | 必填 | 默认 | 不合规时的报错（plan 作业红，verdict 作业发 `failure`） |
|---|---|---|---|---|
| `schema` | 整数 `1` | 是 | — | `plan schema 2: this release reads schema 1` |
| `parts` | 0 到 256 个对象的数组（GitHub matrix 的上限）；空数组合法：没有部件作业，verdict 作业发 `success`，描述 `nothing to run` | 是 | — | `"parts" is a list of at most 256 parts` |
| `files` | 字符串：checkout 下的相对路径（不含 `..`、不以 `/` 开头），一个目录：plan 命令写进去的文件，plan 作业上传一次（artifact `ci-plan-files`），每个部件在命令之前下载，路径在变量 `CIRUNNERS_FILES` 里；目录里不能有符号链接，最多 1000 项、64 MiB | 否 | 无（`CIRUNNERS_FILES` 为空） | `files is a relative path below the checkout, without ".."`；`files: not a directory`；`files: <路径> is a link`；`files: more than 1000 entries or 64 MiB` |
| `max_parallel` | 整数，[1, 256]：本次运行同时跑的部件数，只能**降低** registry 的 `max_parallel`，不能提高 | 否 | registry 的值 | `max_parallel is an integer in [1, 256]` |
| （顶层其他键） | — | — | — | `a plan is an object with "schema" and "parts", and optionally files, max_parallel` |
| `parts[].id` | 字符串，`[A-Za-z0-9][A-Za-z0-9_.-]*`，最多 64 个字符，计划内唯一，不能是 `plan` | 是 | — | `<id>: an id is [A-Za-z0-9][A-Za-z0-9_.-]*, unique in the plan`；`plan: the id is reserved for the plan job`；`...: an id is at most 64 characters` |
| `parts[].command` | 非空字符串，最多 32 KiB，同 `part` 的 `command`（在 checkout 里 `bash -c`） | 是 | — | `<id>: command is a non-empty string`；`<id>: command is at most 32768 bytes` |
| `parts[].needs` | `cpu`、`gpu`、`gpu2`、`timing`、`light` 之一（§4 的表）；`light`：跑在 `ci-light` 上，不占 slot、不用卡，`minutes` 最多 15，从不溢出到别的标签 | 是 | — | `<id>: needs 'tpu' is not one of cpu, gpu, gpu2, timing, light` |
| `parts[].minutes` | 数，(0, 240]（`light` 为 (0, 15]）：参考主机上的时长 | 是 | — | `<id>: minutes is a number in (0, 240]`；`<id>: minutes is a number in (0, 15] for needs light` |
| `parts[].memory_gib` | 正数（GiB），最多 4096 | 否 | registry 的 `memory` | `<id>: memory_gib is a positive number`；`<id>: memory_gib is at most 4096` |
| `parts[].results` | 字符串（`CI_RESULTS_DIR` 下 JUnit XML 的 glob） | 否 | 无 | `<id>: results is a string (a glob)` |
| `parts[].queue_minutes` | 数，(0, 240]：一次尝试在主机上等 slot 或租约的上限（`part` 的 `queue-timeout-minutes`）；到了就是 `unavailable`，按重试的五行处理（同丢失的尝试一样换一台）；作业的 `timeout-minutes` 加上它 | 否 | 15 | `<id>: queue_minutes is a number in (0, 240]` |
| `parts[].artifacts` | 对象，最多 4 项：名字（`[A-Za-z0-9][A-Za-z0-9_.]*`，最多 32 个字符，不含 `-`，不能是 `ci`）→ `RUNNER_TEMP` 下的相对路径；每次尝试结束后（红了也传）上传为 artifact `<名字>-<id>-<尝试>`，保留 7 天，路径不存在只警告；verdict 不读它们 | 否 | 无 | `<id>: artifacts maps at most 4 names ([A-Za-z0-9][A-Za-z0-9_.]*, no "-") to relative paths below RUNNER_TEMP`；`<id>: the artifact name ci is reserved (ci-part-*, ci-plan-files)` |
| （部件的其他键 / 缺键） | — | — | — | `<id>: unknown key(s) ['gpus']`、`<id>: missing ['minutes']` |

- 以上 `files`、`max_parallel`、`light`、`queue_minutes`、`artifacts` 和空的 `parts` 都是 schema 1 的可选扩展：不用它们的计划照旧。
- 标准输出不是一个 JSON 文档：`plan: the plan command's standard output is not one JSON document (JSONDecodeError)`；超过 1 MiB：`plan: the plan is larger than 1048576 bytes`；整个部件列表（路由后）超过 500 KB（远在 GitHub 一个作业输出和一个表达式的 1 MB 之内）：`plan: the routed parts take <n> bytes, more than 500000`；`files` 的路径上有符号链接：`plan: files: <路径> goes through a link`。
- **plan 命令能读到的事件信息**（环境变量，不需要 token）：`CIRUNNERS_EVENT`（事件名）、`CIRUNNERS_PR`（PR 号，非 PR 为空）、`CIRUNNERS_PR_LABELS`（PR 标签名的 JSON 数组，非 PR 为 `[]`）、`CIRUNNERS_BASE_REF`/`CIRUNNERS_BASE_SHA`（PR 的 base；push 时为默认分支和推送前的 sha）、`CIRUNNERS_HEAD_REF`/`CIRUNNERS_HEAD_SHA`（要跑的 head）。plan 命令和部件命令都**拿不到 token**。
- `plan` 命令自己红了是 `failure`（`plan: red (...)`），`unavailable`、`lost` 等是 `error`；路由错误（某个标签没有任何主机提供、内存连 `mem-big` 的 slot 都放不下）是 `error`，描述以 `route: <标签>: part <id>: ...` 开头。这些情况一个部件也不启动，plan 作业把状态交给 verdict 作业去发（plan 作业的 token 只读）。
- 每个部件之后带上路由的结果跑：`labels`（作业的 `runs-on`，只有能力标签）、作业的 `timeout-minutes`（`minutes` × 最慢 slot 的 `speed` + 15，再加 `queue_minutes`）、`gpus`（`gpu` 1、`gpu2` 2、其余 0）、`memory`（`memory_gib` 换成 MiB）。plan 作业的 summary 列出每个部件的 `needs`、标签、分钟数、作业上限和路由的理由（§16 of operations）。
- 一次运行同时最多跑 registry 的 `max_parallel` 个部件（默认 16，1 到 256；design §7 第 1 条），由维护者写在 registry 条目里；计划的 `max_parallel` 只能把它调低。

**`contract`**（决定 16.6）：默认分支的 `.github/ci-runners.toml` 可以写 `contract = N`（非负整数）。plan 作业在运行 head 的任何东西之前，用作业的 token 读默认分支和 head 的这个文件，比较两个值：

- 默认分支没有这个文件或没有 `contract`：不检查；
- head 的值更小、没有 `contract`、文件不存在或读不进来：plan 作业里就结束（verdict 作业发 `failure`），描述形如 `contract 1 here, 2 on main: merge origin/main`，一个部件也不启动，不到 30 秒（plan 和 verdict 两次取 `ci-light`）；
- 默认分支的 `contract` 不是非负整数、或文件读不进来：`error`（默认分支自己的问题）。

改变计划或部件对 head 的要求的 PR，同时把 `contract` 加一；落后的分支 `merge origin/main` 之后就过了。

## 9. 接入可复用工作流 `ci.yml`（design §4.10，决定 16.6，RUN-10 R6 起）

一个仓库接入只要下面这 20 行工作流，加维护者写的一个 registry 条目（§3 第 2 步）：

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:
concurrency:
  group: ci-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: true
jobs:
  ci:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    uses: chaobrain/ci-runners/.github/workflows/ci.yml@v1
    permissions: { contents: read, statuses: write, actions: read, pull-requests: read }
    with:
      context: myproj/ci
      plan: python scripts/ci_plan.py
```

**输入**：`context`（必填，状态上下文）；`plan`（可选，打印计划 JSON 的命令，见 §8；不写就是一个 `pytest` 部件）；`project`（可选，registry 的键，默认仓库名，取自 `GITHUB_REPOSITORY`，所以 `schedule` 也有）；`sha`（可选，要跑的 head，默认 PR 的 head，否则 `github.sha`；给手动派发一个指定的 head 用。它不受检查：调用方自己保证它不是 fork 的 commit）；`plan-only`（可选，布尔，默认 `false`：只做 claim 之外的 plan 作业，即 `contract`、plan 命令、检查、路由和 summary，不跑部件，verdict 发 `success` `plan only: N part(s) routed, none run`；请给它一个单独的 `context`）；`plan-token`（可选，布尔，默认 `false`：把 plan 作业的只读 token 作为 `GH_TOKEN` 交给 plan 命令，且只交给它；部件命令永远拿不到 token；公开仓库的运行本来就被拒绝）。**输出**：`state`，verdict 作业发的状态（`success`、`failure`、`error`），claim 跳过已经绿的 head 时为空。

**一次运行三次取 runner**（关键路径上）：

1. **plan**（`ci-light`，token 只读，不发任何状态）：claim 的判断（已经绿的 head 跳过）→ `contract` 检查（§8）→ checkout head、跑 `plan` 命令 → 检查计划、按能力标签路由 → summary 里列出每个部件。
2. **parts**：每个部件调用一次 `part.yml`，同时最多 registry 的 `max_parallel` 个。部件第一次尝试跑在路由给的标签上；按 `outcome.hint` 的五行（§2 的结果表）最多重试一次；第一次尝试没留下结果（runner 被停、上传失败）时，先在那台主机上 salvage（它的记录只在那台主机上），再决定重试。一次尝试红了，部件的 `red` 小作业（`ci-light`，只有 `statuses: write`，不跑 head 的代码）立刻在 `context` 上发 `failure`（"red at part <id>"），其他部件照常跑完，summary 完整；跑部件命令的作业都没有写权限。 同时一个 **pending** 小作业（`ci-light`，只有 `statuses: write`）发 claim 的 `pending`：只在计划路由出部件、claim 没跳过的时候，并且 claim 读状态之后 runners 没发过别的状态（红色不会被盖掉，绿色也不会）。
3. **verdict**（`ci-light`）：发本次运行唯一的最终状态。plan 作业结束了运行时（`contract` 落后、计划读不进来、plan 命令红了、路由错误、空计划、`plan-only`）发 plan 作业给出的状态和描述；否则读每个部件的 artifact，每个部件以最后一次尝试为准，合并 summary，发状态。plan 作业结束运行的路径上，verdict 是第二次取 runner。

**状态的描述**（精确字符串，测试红和基础设施红分开）：

| 谁发 | 状态 | 描述 |
|---|---|---|
| 部件的一次尝试红了（立刻） | `failure` | `red at part <id>; the other parts run on`；重试红了：`red at part <id> (its retry); the other parts run on` |
| verdict | `success` | `passed: <id>, <id>, ...`，超过 140 个字符时 `passed: all N parts` |
| verdict | `failure`（测试红：有部件最后一次尝试是 `red`） | `red: <id> red, ...` |
| verdict | `error`（基础设施：没有红，但有部件最后是 `lost`、`unavailable`、`oom`、`thermal`、`reboot`、`stopped`） | `infrastructure: <id> <outcome>, ...` |
| verdict（plan 作业结束运行时） | `failure` | `contract ...: merge origin/<分支>`（§8）、`plan: red (...)`、`plan: <计划的错误>` |
| verdict（plan 作业结束运行时） | `error` | `plan: <outcome> (...)`（plan 命令 `lost`、`unavailable` 等）、`route: ...`、`plan: no plan output`、默认分支的 `contract` 读不进来、`a public repository: ci-runners runs private ones only until decision 18 C2` |
| verdict（plan 作业结束运行时） | `success` | `nothing to run`（空的 `parts`）、`plan only: N part(s) routed, none run` |

描述超过 140 个字符时截断，以 `...` 结尾。

`pull_request_target`、fork 的 PR 在 plan 作业的 guard 里就跳过；**公开仓库**现在不能调用 `ci.yml`：它的 `public` 作业在 GitHub 托管的 runner 上直接失败（公开仓库的 CPU 部件走托管 runner、GPU 走隔离的池，等决定 18 的 C2 实现）；事件的 payload 里没有仓库信息时（如 `schedule`），plan 作业的 `contract` 步骤用 API 查仓库是否私有，公开的就结束运行（verdict 作业发 `error`），head 的任何东西都不运行。部件在 GitHub 的队列里一直没有 runner 接的情况，现在没有上限（GitHub 不能取消单个作业；计划中的定时 sweeper 见 issue #213），在那之前请用自己的 sweeper。一个工作流里只调用一次 `ci.yml`（定位丢失尝试的主机时按作业名找 `part <id> / first`）。运行被取消（同一个 PR 推了新 head）时不重试、不发结论，新 head 的运行会发。不要把 secret 传进来：`ci.yml` 不接收也不转发任何 secret；plan 作业的 token 只读（`contents`、`statuses`、`actions`、`pull-requests` 都是 `read`），所以调用方要给 `pull-requests: read`（GitHub 拒绝被调用的作业要的权限超过调用方给的）；跑部件的作业只有 `contents: read`，token 不进部件命令的环境，只在 `plan-token` 打开时以只读的形式进 plan 命令；checkout 不保存凭据。注意：部件命令以 CI 账户运行（design T13），它能改写同一个作业里之后的步骤用到的文件，所以同一作业里的重试决定、作业的输出并不防部件命令本身（plan 命令对 plan 作业的输出也一样）；结论以 verdict 读到的 artifact 和主机记录为准。手动取消的运行不发最终状态，它发过的 pending 留到下一次运行。

**自己组合的部分**（`ci.yml` 不做，design §4.8）：`ci.yml` 只管测试部件和它们的结论。

- 所有部件之后再跑、读部件 artifact 的作业（合并覆盖率、bisect、报告、上传影响图）：在调用 `ci.yml` 的作业后面另加一个作业（`needs: ci`，`if: always()` 视需要），用 `actions/download-artifact` 读部件的 `artifacts`（§8）、用 `part` action 在 `ci-light` 上跑命令；它不计入 verdict。
- 指定集群目标（`target`）、`exclude` 之类的强制选择：`ci.yml` 不提供，需要时按 §3 自己组合 `claim`、`part`、`salvage`、`verdict`，只写能力标签。
- 自己格式的 summary：在自己的作业里写；`ci.yml` 的 summary 格式是固定的。`ci.yml` 自己用的 `actions/flow` 是它内部的步骤，不要在自己的工作流里调用。
