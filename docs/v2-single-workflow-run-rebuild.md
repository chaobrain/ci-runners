# v2.1.0 从头部署

发行源码与部署输入、运行目录分开。统一入口是 `parallel/deploy.sh`；`setup/` 保留旧接口，不用于新的 v2 安装。本文描述待执行的标准流程，不代表其他设备已安装。

## 1. 设备与目录

Linux x86_64，Python 3.11+，可用的 systemd 用户会话，以及 Git、curl、tar、OpenSSL、SSH、rsync。构建项目环境时还需 conda；已有环境可选择 `existing`。本地执行必须实际生效的 CPU 时间配额和内核内存限制，无需 cpuset 委派。Slurm 保留混合 cgroup 识别与每两秒检查命令进程树匿名内存的监控方式。

```text
工作目录/
├── ci_runner_v2.1.0/                # 可复制的固定版本源码；交付目录有独立 .git
├── ci-deploy-input-gpu-3090a/       # 主控部署输入，独立于 Git
└── ci-runtime/
    ├── ci-runners/
    │   ├── releases/v2.1.0/         # 本地源码的独立安装副本
    │   ├── releases/hook -> v2.1.0
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

这一步无需项目、凭证或正在运行的控制服务。完整部署仍按后续步骤填写项目与凭证。配置 GPU 后，控制节点在 plan-0 外生成五个专用等待 runner，分别承接四种指定设备和自动选卡的请求。执行端每次心跳更新健康卡的可用显存。控制节点用 `ci-run central capacity` 查看每种设备的 GPU 总量、空闲、预留、运行中和不可用数量。

升级时先暂停新批次，等中央任务、GitHub job 和本地清理回报全部结束。按主机停止全部旧 runner，检查 Listener、Worker 和任务 scope 已退出，旧卡锁已释放，目标卡没有计算进程。承担 Slurm 驱动的节点还需要核实远端账务中的作业已结束。核对每张卡后按 3090a、3090b、pro6000、h100 的顺序切换。每台先验证一张卡的执行、取消和归还。整版回退前先排空并归档新版记录，再配套恢复备份的程序、配置与两端账本。

在发行源码根目录执行：

```bash
./parallel/deploy.sh init --input ../ci-deploy-input-gpu-3090a
```

`init` 自动建立指定的输入目录、凭证子目录和两个空的手填文件。重复运行会保留已填写的 PAT、注册令牌、主机配置和项目选择。

```text
ci-deploy-input-gpu-3090a/
├── host.toml
├── projects/<project>.toml
├── environments/                       # kept 环境构建记录
├── credentials/
│   ├── pat/github-pat                  # 手填，只有主控读取
│   ├── runner/runner_token             # 手填，本机注册新 runner 时读取
│   ├── tls/issued/                     # 首次主控 apply 自动创建，含 ca.key、ca.crt、server.key、server.crt
│   ├── hosts/<host>                    # 主控 apply 为每台 executor 各生成一个 HTTPS 连接令牌
│   ├── slurm/<account>                 # 启用 Slurm 驱动时放入的受限 SSH 私钥
│   └── adm_token/                      # 可存手工管理凭证，部署脚本不读取
└── slurm/
    ├── slurm1.toml                 # 原有 site 格式
    ├── ssh_config                  # 集群连接别名；仅驱动需要
    └── known_hosts                 # 已核对的集群主机公钥
```

所有凭证文件和 Slurm 连接文件设为 `600`，私有目录设为 `700`。不要把 token 写到命令行、源码、workflow 或聊天中。注册文件只保存令牌，不保存完整 config.sh 命令。

`host.toml` 是拓扑唯一来源：填写组织、已存在的 runner group、controller 的 HTTPS 地址与监听地址，以及各主机的账户、真实 home、独立 runtime、CPU 数量（`cpu_count`）和内存额度。`--host` 选择本机。group 的可访问仓库由组织维护者设置；脚本不更改组织策略。

只保留本次纳管的主机。启用 Slurm 时，将 controller 的 `slurm_runners` 设为需要的数量（现有协议上限 20），并提供站点与密钥。文件 `slurm1.toml` 沿用 `setup/slurm/site.example.toml` 格式；账户别名 `acct1` 的密钥文件名也为 `acct1`。`ssh_config` 中的 Host 对应 site 的 accounts.acct1.ssh，例如：

```sshconfig
Host slurm1-acct1
    HostName cluster.example.invalid
    User replace-account
    Port 22
```

运行时将其安装到自己的 etc/slurm，通过独立 SSH 配置使用，保留原有受限提交接口；不修改用户全局 SSH 配置。known_hosts 从已核对的主机密钥填写。

项目 TOML 选择私有仓库、context、明确准入的 actors、devices、资源和重试限制。当前旧 policy 使用一份主机级 actors 名单，因此同部署的项目应使用相同信任集合，避免隐式扩大准入。repository_id 可由 controller 通过 PAT 查询，随后写入执行节点导出包。

环境模式：`build` 在 controller 上复用原环境构建器读取可信默认分支的规格；`export-host` 携带构建记录并将执行端改成 `kept`。`kept` 从 input 内的 record 路径重建，不传递 PAT；上游仍沿用旧构建器按声明分支获取的行为，构建记录保存实际提交。已有环境用 `existing`，在 `[hosts.<设备>]` 指定 env，或放在自动生成的 runtime/ci-runners/envs/<project>。环境刷新仍使用原 `ci-run env refresh`，重复 apply 不自动刷新已成功构建的环境。

## 3. 凭证用途

controller 的 fine-grained PAT：对允许的项目需要 Metadata 读取、Actions 读取、Pull requests 读取、Commit statuses 读写；使用环境构建器读取私有源码时需要 Contents 读取，并涵盖必要的私有上游。可选组织权限 Self-hosted runners 读取用于真实在线状态查询；没有时显示 unknown。

注册使用组织提供的短期 registration token，目标 URL 是 `https://github.com/chaobrain`。安装器不通过 PAT 创建注册令牌，所以常驻 PAT 不需要仓库 Administration 或组织 runner 写权限。更新源码和 workflow 的 Contents/Workflows 写权限属于发布操作，用户在其他位置推送时使用。

主控第一次 `apply` 时，会把内部 CA 和服务端密钥、证书保存到 `credentials/tls/issued/`，把各执行节点的独立令牌保存到 `credentials/hosts/<host>`。也可以在部署前将已有的 `ca.crt`、`server.crt`、`server.key` 一起放入 `credentials/tls/`，服务端证书必须覆盖 `controller.url` 的主机名或 IP。服务读取运行目录 `parallel/etc/private/` 中的安装副本。重复部署复用已签发的身份，首次签发不会使计划失效。

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

controller 安装成功后分别导出：

```bash
./parallel/deploy.sh export-host --host 3090a --target 3090b --input ../ci-deploy-input-gpu-3090a --output ../ci-deploy-input-gpu-3090b
```

将发行源码和这份专用导出目录复制到 3090b。导出会建立相同的凭证子目录，只有 `credentials/hosts/3090b` 和 `credentials/tls/ca.crt` 带有主控签发的连接身份。3090b 的 `credentials/pat/github-pat` 留空即可。首次注册前，在 **3090b 自己的** `credentials/runner/runner_token` 填入有效的组织注册令牌，然后在 3090b 执行：

```bash
./parallel/deploy.sh check --host 3090b --input ../ci-deploy-input-gpu-3090b
./parallel/deploy.sh plan --host 3090b --input ../ci-deploy-input-gpu-3090b --output ../plan-3090b.json
./parallel/deploy.sh apply --host 3090b --input ../ci-deploy-input-gpu-3090b --plan ../plan-3090b.json
```

注册完成后可以清空或移走这台机器的注册令牌文件。已有 runner 使用运行目录中的注册身份；新增或重注册 runner 时再填入新令牌。pro6000 和 h100 使用各自的导出目录和 `--host`。

执行节点向 controller 主动发起 HTTPS 请求，正常每 5 秒同步，失败退避最多 30 秒，心跳有效期 120 秒。网络需允许节点访问 controller.url，并允许 runner 出站连接 GitHub。各服务使用 Restart 策略；不需要 SSH 作为日常任务控制通道，不自动 git pull main。

status 区分用户服务、最近控制同步、Listener 进程和 GitHub 在线状态。断线继续监督已启动任务并保留结果；恢复后先核对账本，不能凭超时清空资源。日志查看：

```bash
journalctl --user -u ci-parallel-scheduler.service -u ci-parallel-executor.service -n 80
```

## 6. Slurm 集群程序安装

只有受限 CI 密钥时，生成维护包交给持有普通登录权限的维护者：

```bash
./parallel/deploy.sh slurm-package --host 3090a --input ../ci-deploy-input-gpu-3090a --site slurm1 --account acct1 --output ../slurm1-package
```

维护包不含私钥。上传并在集群登录账户下依次运行 `./install.sh`、`./install.sh --apply`。程序检查账号路径、Slurm 条件及未结束的 CI 作业，再安装完整 release 和原 gate 配置；必要时按生成的 authorized-key.txt 安装受限公钥行。普通 SSH 登录可继续使用原 `ci-run slurm check/plan/apply --via` 流程。

集群项目环境继续使用原 `ci-run slurm env` 和 canary 流程；维护包负责集群程序，不夹带设备专用 conda 环境。最后用 CI 提交密钥验证提交、资源限制、结果取回和取消；不通过受限 key 执行维护 shell。

## 7. 发布与验收

源码版本、包版本、action 标记和 workflow 自引用均为 2.1.0。用户将交付分支推送、合并并发布 `v2.1.0` 后，消费方才可以解析模板中的固定版本引用。消费方模板为 `parallel/examples/consumer-workflow.yml`。

本地验收和实机验收分开记录。实机至少验证：唯一 plan、各节点独立控制连接、组织 runner group 准入；同一 run 内的 plan、测试和汇总；超过容量的排队、失败、取消、重启和迟到回报；Slurm 的 CPU 与进程树内存监控、取消和清理。尚未验证的远端设备不标记为已上线。

备份数据库时先排空、停止控制服务，再配套保存 scheduler/executor 数据库及 WAL/SHM、部署输入和私密身份。恢复时保持控制端与执行端记录对应；不只删除一侧账本。发生部署失败先查看执行记录和 journal，保留已注册 runner 与未释放任务；代码切换和配置恢复在排空停服后进行。Slurm 花费账目独立保留。
