# 使用指南：在 h100 上跑仓库的测试

这份指南写给要接入这套 CI 的仓库。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。维护主机的人请看 [operations.md](operations.md)。

## 1. 这是什么，谁能用

- chaobrain 的**每个私有仓库**都能用。h100 上的 runner 跑测试，CPU 和 GPU 两部分都在 h100 上（design 决定 11）。
- **只有 org 成员**触发的任务才会被接受。成员和私有仓库的名单每 10 分钟从 org 自动同步一次（design §6.5），不需要找人加白名单。
- **公开仓库不能用**，继续用 GitHub 托管 runner。org 也不允许 fork 私有仓库，所以 fork 的 PR 跑不到这里。
- 私有仓库调用 `h100-test.yml` 时，所有作业都跑在 h100 上，**不消耗**私有仓库的托管 Actions 分钟数。

**共享账户意味着什么。** h100 的 runner 跑在一个实验室多人共用的 Unix 账户上（design §6.1）。任务运行期间，其他使用者能看到你的 checkout，也有能力篡改结果。所以：

- 不要把 secret 传给这些任务；
- 代码不能给同实验室的人看的仓库，不要接入。

## 2. 管线

```
push / PR（同仓库）/ workflow_dispatch
        │
        ▼
claim          [self-hosted, h100-light]
  这个 head 已有结论就跳过（绿的不重跑），否则发 pending
        │
        ▼  （每个作业开跑前，runner 上的 pre-job hook 先检查：
        │    org 成员？快照里的私有仓库，且 id 对得上？快照 ≤ 60 分钟？不是 fork？
        │    不满足就在 "Set up runner" 里拒绝，打印 ci-runners: refused <原因>）
        ├──────────────────────────────┐
        ▼                              ▼
cpu   [self-hosted, h100-cpu]     gpu   [self-hosted, h100]（一张卡一个 runner）
  CI 槽位 → scope 内运行             健康门：卡 < 80 °C、watchdog 无告警
  内存上限、超时                     租卡 → scope 内运行，CUDA_VISIBLE_DEVICES=N
                                     每 30 s 采样；≥ 85 °C 持续 3 min 提前停止
        │                              │
        ▼                              ▼
cpu-retry / gpu-retry：结果要求重试时（oom、thermal、unavailable 等）重试一次；
  GPU 部件换到 retry-on 指定的凉卡
        │
        ▼
verdict        [self-hosted, h100-light]
  汇总各部件最后一次尝试的结果；没有 artifact 的部件从主机记录中 salvage；
  合并 summary，发 <project>/ci 状态：success / failure / error
```

**部件的结果**（design §4.4）：

| 结果 | 含义 | 之后 |
|---|---|---|
| `pass` | 命令退出码 0 | — |
| `red` | 命令失败或超时，不是基础设施的原因 | 状态为 failure |
| `oom` | 超出内存上限，被杀 | 带 `CI_RESUME=1` 重试一次 |
| `thermal` | 卡过热，被 watchdog 杀掉或被提前停止 | 换一张凉卡重试一次 |
| `reboot` | 运行期间主机重启 | 同 thermal |
| `unavailable` | 开跑前就不满足条件：卡太热、没有空卡、排队超时、磁盘不足、`gpus > 1` 等 | 重试一次 |
| `lost` | 作业没留下输出就结束了，例如 runner 被停 | verdict 从主机上的记录 salvage |
| `stopped` | 被取消，例如同一个 PR 推了新 head | — |

只有 `red` 会让部件这一步退出 1。基础设施问题重试一次还没解决，状态就是 `error`，不是 `failure`。

## 3. 接入步骤

1. **写环境规格。** 在默认分支上加 `.github/ci-runners.toml`（见下文）。规格只从默认分支读取，PR 改它不会影响环境。
2. **请维护者建环境。** 维护者在 h100 上写 registry 条目 `etc/projects/<project>.toml`，然后跑 `ci-run env build <project>`，步骤见 [operations.md](operations.md) §4。依赖改动合并之后要重建一次。
3. **加工作流**（见下文），引用 `@v1`。
4. **金丝雀验证。** 在一个分支上分别放一个必然失败的 CPU 测试和一个必然失败的 GPU 测试，确认它们各自只在自己的部件里变红。
5. **完成。** 连续几个 head 端到端跑通，`<project>/ci` 状态和 summary 都对。

**`.github/ci-runners.toml`**（schema 1）：

```toml
schema = 1
python = "3.12"                                   # 环境的 Python 版本（conda）
requirements = ["requirements-dev.txt"]           # pip -r，按顺序；仓库内的路径，-r/-c 引用的文件会一并读取
install = ["pytest-xdist"]                        # 额外的依赖说明符：不能带选项、URL 或路径
[[upstream]]                                      # 可选：以 editable 方式安装的兄弟仓库
repository = "chaobrain/brainstate"
ref = "origin/main"
[variables]                                       # 写进 registry 条目的环境变量（保留名会被拒绝）
XLA_PYTHON_CLIENT_PREALLOCATE = "false"
```

项目本身**不会**装进环境：部件在作业的 checkout 里运行，`python -m pytest` 会直接从 checkout 导入项目。upstream 在建环境时 clone 并以 editable 方式安装，版本是当时分支的 head。

**工作流**（`.github/workflows/test.yml`）：

```yaml
name: test
on:
  pull_request: {}
  push: {branches: [main]}
  workflow_dispatch: {}
concurrency:
  group: test-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: true
permissions: {contents: read}
jobs:
  test:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    uses: chaobrain/ci-runners/.github/workflows/h100-test.yml@v1
    permissions: {contents: read, statuses: write}
    with:
      project: myproj
      cpu-command: python -m pytest -m "not gpu" --junitxml="$CI_RESULTS_DIR/cpu.xml"
      gpu-command: python -m pytest -m gpu --junitxml="$CI_RESULTS_DIR/gpu.xml"
      results: "*.xml"
      timeout-minutes: 60
```

注意：

- `concurrency` 必须写成块格式。行内映射 `{group: ...${{ }}...}` 里含表达式时，YAML 会解析失败，GitHub 会直接拒绝整个文件。`tests/test_workflow.py` 会检查本文档里的示例。
- `permissions` 在调用处要给 `contents: read` 和 `statuses: write`：claim 和 verdict 需要发状态。

**`h100-test.yml` 的输入**（design §4.7）：

| 输入 | 默认 | 含义 |
|---|---|---|
| `project` | 必填 | registry 键，必须与仓库对应 |
| `cpu-command` | 空 | CPU 部件的命令；为空就不跑这个部件 |
| `gpu-command` | 空 | GPU 部件的命令；为空就不跑这个部件 |
| `gpus` | 1 | GPU 部件用几张卡；h100 上只能是 1 |
| `timeout-minutes` | 60 | 每个部件的内层超时，1 到 240；作业的超时再加 15 分钟 |
| `results` | 空 | `CI_RESULTS_DIR` 下 JUnit XML 的 glob |
| `context` | `<project>/ci` | claim 读、verdict 写的状态 context |

两个命令至少要设一个。

## 4. 命令约定

部件的命令在作业的 checkout 里用 `bash -c` 运行。项目环境已经放在 `PATH` 最前面，并设好了 `CONDA_PREFIX`。它应当：

- 只在测试失败时以非零退出；
- 收到 TERM 时停掉自己的进程树（超时会先发 TERM，30 秒后发 KILL）；
- 用 `CUDA_VISIBLE_DEVICES` 给的卡（GPU 部件一张卡；CPU 部件为空），不自己选卡；进程已经用 taskset 绑到 `CI_CPUS` 列出的 CPU（NUMA 0），定并行度时以它为准；
- 把 JUnit XML 写进 `$CI_RESULTS_DIR`，结果会出现在 summary 里；
- 能续跑的话，支持 `CI_RESUME=1`（OOM 之后的重试会带上它）。

命令还能读到这些环境变量：

- `CI_PROJECT`、`CI_PART_DIR`、`CI_RUNNERS_HOST`；
- registry 里的 `variables`；
- runner 的 `https_proxy`、`http_proxy`、`no_proxy`。h100 访问外网要经过 egress，所以 pip、git、下载数据集都会自动走代理；只能访问 egress 允许清单上的域名（见 [operations.md](operations.md) §6）。

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
  | `runner-class` | 这个 runner 不接这一类任务 |
  | `policy` / `timeout` / `error` | hook 无法做出判断，请联系维护者 |

- **新建的私有仓库或新成员**：最多等 10 分钟，下一次同步后就能用。
- **想对同一个 head 重跑**：已经绿的 head 会被 claim 跳过，要重跑就推一个新 commit。红的 head 或 error 的 head 重新触发会再跑一次。
- **`unavailable: health gate`**：分到的卡太热，会自动换一张凉卡重试。
- **作业排队很久**：CPU 部件共用 2 个 CI 槽位，GPU 部件一张卡一次只跑一个。每个项目在每个池子里最多有 1 个等待中的部件，再多就直接 `unavailable`（"project queue full"）。
