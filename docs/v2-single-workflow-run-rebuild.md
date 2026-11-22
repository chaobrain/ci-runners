# v2.1.2 从头部署

发行源码与部署输入、运行目录分开。统一入口是 `parallel/deploy.sh`；`setup/` 保留旧接口，不用于新的 v2 安装。本文描述待执行的标准流程，不代表其他设备已安装。


## 私有仓库组织模式

主控维护 `host.toml`、`organization.toml` 和
`device-admission.toml`。各执行器维护自己的 `host.toml`。凭证继续按下文
的目录单独提供。使用 `parallel/examples/` 下的
`host-organization.example.toml` 和 `host-executor-organization.example.toml`
填写本机参数。两种角色都在 `host.toml` 顶层设置
`schema = 2` 和 `mode = "organization"`。主控设置
`[github].organization` 和用于私有仓库的 runner group。只使用本地设备时，保留 `slurm_runners = 0`。
先准备 `host.toml`，再用下文的 `deploy.sh init` 建立输入目录的其余模板。

`organization.toml` 使用 `organization.example.toml` 的四个参数。
`max_parallel` 限制单次运行的并行任务数，`max_minutes` 限制单个任务时间，
`retries` 允许重试一次或禁用，`gpu_mem_mib` 设定 GPU 空闲显存下限。
组织模式的新模板在每台参与主机的 `[hosts.<本机>]` 配置 `env_cache`，指向可写的本机缓存目录。设置 `conda` 为本机 Conda 的绝对路径。主控使用的 Python 须能在隔离模式下导入 PyYAML。空缓存即可通过部署检查。需要平台变体的主机可设置 `env_cache_variant`。

每个仓库在可信默认分支维护 `.github/ci-environment.yml`。主控为每个 run attempt 和状态 context 固定 YAML、引用的 requirements/constraints 和解析后的上游提交。PR 代码仍从通过身份验证的测试提交检出。仓库的所有分片继承同一默认规格。工作流无须传环境路径或清单。

计划机准备自己的环境后执行 plan 命令。pytest 项目可以使用 `"$CI_PYTEST_PLAN" --shards 24 --cores 4 --workers 1 --minutes 20 -- tests` 生成按用例数平均分配的 CPU 任务。执行器通过认证心跳报告支持缓存模式。设备候选和重试由资源、优先级、显式设备和 Slurm 准入决定。执行端接单后检查自己的缓存；同一规格的并发冷启动只构建并发布一次。构建占用该任务的时间和资源额度，按首次安装实测为 `minutes` 留出时间。

已有 `env_root` 部署继续按仓库名或 `env_aliases` 选择预置环境，并在分配时检查就绪状态。已有 `env` 部署继续使用共享前缀。切换缓存模式时将主控和执行器改为 `env_cache`；使用 `env_legacy_root` 可指定 v1 环境根，`env_aliases` 可提示旧环境目录。执行器核对环境规格、依赖文件和上游提交后才采用旧前缀。

上线时按下文的 `check`、`plan`、`apply` 顺序切换主控和执行器。
每个要跑 CI 的私有仓库继续在自己的 workflow 里调用 `ci-v2.yml`，
提供 `context` 和输出 schema 2 计划的 `plan` 命令。GitHub 组织维护者
设置 runner group 的仓库范围。主控 PAT 需能读取目标仓库的 Metadata、
Actions、Pull requests，并读写 Commit statuses。组织模式无需查询成员或
协作者的有效权限。

`runtime/output/repositories.json` 记录 PAT 可见的组织仓库全名、ID、
可见性和同步时间。主控启动时同步，此后每 10 分钟更新一次。
私有任务由主控实时核对 GitHub 仓库归属、可见性、ID 和运行信息。
组织任务未指定设备时，主控按 `h100 → pro6000 → 3090b → 3090a → slurm` 选择首个能承接任务的设备。GPU 任务仅使用前四个设备。显式指定的设备在重试时仍为硬约束。

启用集群 CPU 后备时，在兼任 executor 的主控设置 `slurm_runners`（1 至 20），按第 2 节准备站点、受限 SSH 密钥和 known_hosts。管理员在该机部署输入的根目录维护 `slurm-allowlist.json`（权限 600），例如

```json
{
  "schema": 1,
  "repositories": [
    {
      "repository_id": 12345,
      "repository": "example/private-tests",
      "slurm_project": "tests",
      "slurm_target": "cluster1/cpu"
    }
  ]
}
```

填写 GitHub 核实的仓库 ID 和全名。站点的 CPU 目标须允许该项目。预置模式需要集群已有 `envs/<slurm_project>-<variant>`。缓存模式使用集群账户的共享目录 `envs/cache`，与本地设备分别构建。主控在分配和领取时重查白名单，更新时用完整文件原子替换。移出条目的仓库仍可运行本地 CI。公开仓库沿用原有公开 workflow。

接入新设备时，在新机配置本机 `host.toml`、环境缓存或预置环境及凭证，用现有部署命令
启动。主控更新 `device-admission.toml` 和对应设备密钥即可在线授权。
设备登记、runner 在线并报告相应执行能力后即可参与分发，主控无需重启。

下文的逐项目 TOML 适用于项目部署。两种模式共用同一套 Slurm 站点配置、凭据和维护工具。

## 1. 设备与目录

Linux x86_64，Python 3.11+，可用的 systemd 用户会话，以及 Git、curl、tar、OpenSSL、SSH、rsync。构建项目环境时还需 conda；已有环境可选择 `existing`。本地执行必须实际生效的 CPU 时间配额和内核内存限制，无需 cpuset 委派。Slurm 保留混合 cgroup 识别与每两秒检查命令进程树匿名内存的监控方式。

```text
工作目录/
├── ci_runner_gpu_q-2.1.2/           # 源码包解压后的发行目录
├── ci-deploy-input-gpu-3090a/       # 主控部署输入，独立于 Git
└── ci-runtime/
    ├── ci-runners/
    │   ├── releases/v2.1.2/         # 本地源码的独立安装副本
    │   ├── releases/hook -> v2.1.2
    │   ├── etc/                    # 自动生成 inventory、policy、projects、Slurm 配置
    │   ├── hooks/
    │   ├── envs/
    │   ├── cache/
    │   └── state/                  # 旧执行层结果和 Slurm 账务
    ├── parallel/
    │   ├── etc/central.toml
    │   ├── etc/private/            # 实际使用的凭证、CA、节点令牌
    │   ├── state/                  # scheduler/executor SQLite、部署记录、健康状态
    │   └── run/                    # 本地 Unix socket 和部署锁
    ├── output/                     # TOML、JSON 和 SQLite 的查看副本
    ├── actions-runner/<runner>/
    └── ci-work/<runner>/
```

服务输出使用用户 journal；无需另建重复日志守护进程。systemd unit 位于部署账户的 `~/.config/systemd/user/`。3090a 是唯一 controller，运行 plan-0、scheduler、executor；其余设备只运行 executor。CPU 组仍为 4 个逻辑 CPU / 4 GiB；主机额度须由维护者实测填写，脚本不会把样例当成硬件探测结果。

已有设备须先暂停入口、排空本地与 Slurm 作业、注销将迁移的仓库级 runner，并停止旧服务。保留必要的账务与数据库备份。安装器拒绝接管无记录的 unit、runner 和配置，不自动删除旧部署。相同版本但内容不同的试部署使用另一个运行目录；不要覆盖活跃 release。

## 2. 初始化和填写输入

仅登记本机时，外置 `host.toml` 只填写本机的主机块。在该块将维护者选定的完整物理卡 UUID 写入 `gpu_uuids`。逐机用 `nvidia-smi --query-gpu=index,uuid --format=csv,noheader` 核对。pro6000、3090a、3090b、h100 的上限分别为 3、2、2、3 张。缺省和空列表均为零卡。可先只校验拓扑和本机实卡：

```bash
./parallel/deploy.sh check --host 3090a --inventory-only --input ../ci-deploy-input-gpu-3090a
```

这一步无需项目、凭证或正在运行的控制服务。完整部署仍按后续步骤填写项目与凭证。采用 schema 2 时，控制节点在 plan-0 外预先生成五个专用等待 runner，分别承接四种指定设备和自动选卡的请求。执行端每次心跳更新健康卡的可用显存。控制节点用 `ci-run central capacity` 查看每种设备的 GPU 总量、空闲、预留、运行中和不可用数量。

升级时先暂停新批次，等中央任务、GitHub job 和本地清理回报全部结束。按主机停止全部旧 runner，检查 Listener、Worker 和任务 scope 已退出，旧卡锁已释放，目标卡没有计算进程。承担 Slurm 驱动的节点还需要核实远端账务中的作业已结束。核对每张卡后按 3090a、3090b、pro6000、h100 的顺序切换。每台先验证一张卡的执行、取消和归还。整版回退前先排空并归档新版记录，再配套恢复备份的程序、配置与两端账本。

在发行源码根目录执行：

```bash
./parallel/deploy.sh init --input ../ci-deploy-input-gpu-3090a
```

`init` 自动建立指定的输入目录、凭证子目录、两个空凭证文件和 controller 的设备准入模板。重复运行会保留已填写的 PAT、注册令牌、主机配置和项目选择。

```text
ci-deploy-input-gpu-3090a/
├── host.toml
├── device-admission.toml              # controller 的设备准入名单
├── projects/<project>.toml
├── environments/                       # kept 环境构建记录
├── credentials/
│   ├── pat/github-pat                  # 手填，只有主控读取
│   ├── runner/runner_token             # 手填，本机注册新 runner 时读取
│   ├── tls/issued/                     # 首次主控 apply 自动创建，含 ca.key、ca.crt、server.key、server.crt
│   ├── hosts/<host>                    # 设备独立连接令牌，主控与相应设备各持一份
│   ├── slurm/<account>                 # 启用 Slurm 驱动时放入的受限 SSH 私钥
│   └── adm_token/                      # 可存手工管理凭证，部署脚本不读取
└── slurm/
    ├── slurm1.toml                 # 原有 site 格式
    ├── ssh_config                  # 集群连接别名；仅驱动需要
    └── known_hosts                 # 已核对的集群主机公钥
```

所有凭证文件和 Slurm 连接文件设为 `600`，私有目录设为 `700`。不要把 token 写到命令行、源码、workflow 或聊天中。注册文件只保存令牌，不保存完整 config.sh 命令。

每台设备使用一份 `schema = 2` 的 `host.toml`。填写共同的 GitHub 组织、runner group 和 controller HTTPS 地址。`[hosts.<本机>]` 填本机账户、home、runtime 和资源。controller 另外填写 `bind`。执行器填写 `cpu_count`、`memory_max`、`runner_count` 和 `priority`，并按物理卡填写 `gpu_uuids`。`runner_count` 等于 CPU 组数，项目模式按优先级数值降序选用设备。组织模式使用上文约定的固定顺序。controller 和执行器示例位于 `parallel/examples/`。Runner group 的仓库范围由 GitHub 组织维护者设置。

只保留本次纳管的主机。启用 Slurm 时，将 controller 的 `slurm_runners` 设为需要的数量（现有协议上限 20），并提供站点与密钥。文件 `slurm1.toml` 沿用 `setup/slurm/site.example.toml` 格式；账户别名 `acct1` 的密钥文件名也为 `acct1`。`ssh_config` 中的 Host 对应 site 的 accounts.acct1.ssh，例如：

```sshconfig
Host slurm1-acct1
    HostName cluster.example.invalid
    User replace-account
    Port 22
```

运行时将其安装到自己的 etc/slurm，通过独立 SSH 配置使用，保留原有受限提交接口；不修改用户全局 SSH 配置。known_hosts 从已核对的主机密钥填写。

项目 TOML 填写私有仓库、context、devices、资源和重试限制。controller 使用 GitHub PAT 查询仓库 ID，并核验作业涉及账号的仓库成员资格。新部署的项目无需填写 `actors` 或 `repository_id`。

controller 的 `build` 模式读取可信默认分支的环境规格。独立执行器可用 `existing`，在项目的 `[hosts.<本机>]` 指定 env 或使用 `runtime/ci-runners/envs/<项目名>`，也可用 `kept`，从本机输入目录下的构建记录重建环境。环境刷新继续使用 `ci-run env refresh`。

## 3. 凭证用途

controller 的 fine-grained PAT：对允许的项目需要 Metadata 读取（覆盖仓库成员资格查询）、Actions 读取、Pull requests 读取、Commit statuses 读写；使用环境构建器读取私有源码时需要 Contents 读取，并涵盖必要的私有上游。可选组织权限 Self-hosted runners 读取用于真实在线状态查询；没有时显示 unknown。

注册使用组织提供的短期 registration token，目标 URL 是 `https://github.com/chaobrain`。安装器不通过 PAT 创建注册令牌，所以常驻 PAT 不需要仓库 Administration 或组织 runner 写权限。更新源码和 workflow 的 Contents/Workflows 写权限属于发布操作，用户在其他位置推送时使用。

controller 第一次 `apply` 时，将内部 CA 和服务端证书保存在 `credentials/tls/issued/`。也可以在部署前将已有的 `ca.crt`、`server.crt` 和 `server.key` 一起放入 `credentials/tls/`。服务端证书必须覆盖 `controller.url` 的主机名或 IP。

在 controller 的 `device-admission.toml` 增加设备条目。每个设备使用独立连接密钥，`token_file` 相对于输入目录。兼任执行器的 controller 也需要本机条目。

```toml
[devices.3090b]
token_file = "credentials/hosts/3090b"
```

把同一台设备的令牌和 controller 的 CA 安装到该设备的 `credentials/hosts/<本机>` 和 `credentials/tls/ca.crt`。

新目录优先。已有部署仍可读取旧的 `credentials/github-pat`、`credentials/runner-registration-token`、`credentials/host-token` 和顶层 `tls/`。已有旧版手填文件时，`init` 会保留它并显示正在使用的路径。升级时会复用原已签发的 CA 和节点令牌。

## 4. 控制节点安装

```bash
./parallel/deploy.sh check --host 3090a --input ../ci-deploy-input-gpu-3090a
./parallel/deploy.sh plan --host 3090a --input ../ci-deploy-input-gpu-3090a --output ../deployment-plan.json
```

check 输出检查结果；`--offline` 只跳过 GitHub/远端控制查询，本机资源限制仍会检查。plan 生成可阅读的 JSON（变更内容和摘要，不含秘密值）。资源探针以当前普通用户检查 CPUQuota 和 MemoryMax，失败时停止部署。无人登录时继续运行服务需要主机事先启用 linger；这与 CPU 限额无关，部署程序不会自动提权或更改系统设置。旧 `cpus` 配置仅用于计算数量，新配置请填写 `cpu_count`。升级前应排空旧执行任务并确认报告已送达。

确认配置和主机检查通过后：

```bash
./parallel/deploy.sh apply --host 3090a --input ../ci-deploy-input-gpu-3090a --plan ../deployment-plan.json
./parallel/deploy.sh status --host 3090a --input ../ci-deploy-input-gpu-3090a
```

apply 从当前本地源码安装，不要求预先上传 GitHub。按计划准备环境和凭证、生成配置、注册组织 runner、启动控制服务，确认控制连接后启动 runner。源码、配置或凭证改变使旧计划失效；更新过期 registration token 可直接重试原计划。已注册身份和账本保留，失败步骤从执行记录续跑。改变已运行的配置前先排空并停止控制服务。

## 5. 执行节点安装与保活

controller 就绪后，在 3090b 按 `host-executor.example.toml` 填写本机输入，准备项目和现有环境或重建记录，并安装设备连接令牌、CA 及首次注册用的 GitHub 组织 runner 令牌。然后在 3090b 执行：

```bash
./parallel/deploy.sh check --host 3090b --input ../ci-deploy-input-gpu-3090b
./parallel/deploy.sh plan --host 3090b --input ../ci-deploy-input-gpu-3090b --output ../plan-3090b.json
./parallel/deploy.sh apply --host 3090b --input ../ci-deploy-input-gpu-3090b --plan ../plan-3090b.json
```

注册完成后可以清空或移走这台机器的注册令牌文件。已有 runner 使用运行目录中的注册身份；新增或重注册 runner 时再填入新令牌。其他执行器使用各自的本机输入目录和 `--host`。仍使用 schema 1 的部署可以继续使用 `export-host`。

执行节点通过 HTTPS 向 controller 注册，注册成功后正常每 5 秒同步，失败退避最多 30 秒，心跳有效期 120 秒。网络需允许节点访问 controller.url，并允许 runner 出站连接 GitHub。各服务使用 Restart 策略；不需要 SSH 作为日常任务控制通道，不自动 git pull main。

status 区分用户服务、最近控制同步、Listener 进程和 GitHub 在线状态。断线继续监督已启动任务并保留结果；恢复后先核对账本，不能凭超时清空资源。日志查看：

```bash
journalctl --user -u ci-parallel-scheduler.service -u ci-parallel-executor.service -n 80
```

成功部署后，在各机的 `runtime/output/` 查看本机登记和状态。用 `deploy.sh output --host <本机> --input <输入目录>` 刷新副本。controller 的 `devices.json` 随登记与心跳更新。

## 6. Slurm 集群程序安装

只有受限 CI 密钥时，生成维护包交给持有普通登录权限的维护者：

```bash
./parallel/deploy.sh slurm-package --host 3090a --input ../ci-deploy-input-gpu-3090a --site slurm1 --account acct1 --output ../slurm1-package
```

维护包不含私钥。上传并在集群登录账户下依次运行 `./install.sh`、`./install.sh --apply`。程序检查账号路径、Slurm 条件及未结束的 CI 作业，再安装完整 release 和原 gate 配置；必要时按生成的 authorized-key.txt 安装受限公钥行。普通 SSH 登录可继续使用原 `ci-run slurm check/plan/apply --via` 流程。

集群项目环境继续使用原 `ci-run slurm env` 和 canary 流程；维护包负责集群程序，不夹带设备专用 conda 环境。最后用 CI 提交密钥验证提交、资源限制、结果取回和取消；不通过受限 key 执行维护 shell。

## 7. 发布与验收

源码版本、包版本、action 标记和 workflow 自引用均为 2.1.2。用户将交付分支推送、合并并发布 `v2.1.2` 后，消费方才可以解析模板中的固定版本引用。消费方模板为 `parallel/examples/consumer-workflow.yml`。

本地验收和实机验收分开记录。实机至少验证：唯一 plan、各节点独立控制连接、组织 runner group 准入；同一 run 内的 plan、测试和汇总；超过容量的排队、失败、取消、重启和迟到回报；Slurm 的 CPU 与进程树内存监控、取消和清理。尚未验证的远端设备不标记为已上线。

备份数据库时先排空、停止控制服务，再配套保存 scheduler/executor 数据库及 WAL/SHM、部署输入和私密身份。恢复时保持控制端与执行端记录对应；不只删除一侧账本。发生部署失败先查看执行记录和 journal，保留已注册 runner 与未释放任务；代码切换和配置恢复在排空停服后进行。Slurm 花费账目独立保留。
