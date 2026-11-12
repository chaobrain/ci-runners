# 运维手册

这份手册写给维护主机和发布版本的人。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。仓库怎么接入，看 [consumers.md](consumers.md)。

本仓库公开，文档里**不写任何清单信息**：主机名、ssh 别名、IP、账户名、home 路径、令牌都不出现，只用角色名（design §3.3），由 `tests/test_no_inventory.py` 把关。下文的 `<data>` 指主机上 CI 账户数据目录的根（inventory 的 `data`）。

## 1. 拓扑

| 角色 | 跑什么 | 以谁的身份 |
|---|---|---|
| **3090b**、**3090a** | 各自的 runner 池（`<host>-light`、`-cpu`、`-gpu<N>`、`-multi`，3090b 另有 `-compile`，3090a 另有 `-nightly`，design §5.1）；`ci-tunnel.service`（到 egress 的 ssh 转发）；`ci-reaper.timer`、`ci-envrefresh.timer` | 专用 CI 账户的 systemd user 单元，文件都在 `<data>` 下 |
| **h100** | 自己的 runner 池（`h100-light` 8 个、`h100-cpu` 9 个、`h100-gpu<N>` 5 个，design 决定 15.8）；`ci-tunnel.service`；`ci-reaper.timer`、`ci-envrefresh.timer`、`ci-hostload.timer` | 共享账户的 systemd user 单元，文件都在我们的子目录 `<data>` 下（决定 11.3；12.17 退出，15.8 回来）。没有私有 `/tmp`，没有根步骤 |
| **egress 主机** | `ci-egress.service`：HTTP CONNECT 代理，只放行允许清单里的域名的 443 端口 | root 安装，DynamicUser 运行 |
| **工作站** | `ci-orgsync-push.timer`：每 10 分钟用维护者的 gh 登录拉 org 快照，经 ssh 推到每台主机 | 维护者本人的 user 单元 |
| **3090b 上的维护者账户** | `ci-nightly-backstop.timer`：UTC 21:00–22:45 每 15 分钟一次，外加 22:59，用维护者的 gh 登录、经主机的代理包装，在 brainspike 的主 checkout 里跑 `merge_gate.py --backstop`：当晚 20:00 UTC 以来没有 nightly、也没有排队或在跑的，就在 `main` 上派发一次（§12） | 维护者本人的 user 单元 |
| **3090b 上的维护者账户** | `ci-runner-watch.timer`：每 15 分钟一次（UTC :00/:15/:30/:45），用维护者的 gh 登录、经主机的代理包装读 org 的自托管 runner，按主机分组；某台主机连续两次检查都没有在线的 runner，就在 ci-runners 开一个 issue `No runner of <host> online`，恢复后评论并关闭（§13） | 维护者本人的 user 单元 |

数据流：

- GitHub 把任务派给主机的 runner。（h100 于 2026-11-07 退出 CI（design 决定 12.17），2026-11-11 以共享账户回来（决定 15.8）。）
- light runner 共约 20 个（3090b 6、3090a 6、h100 8），每个都带共享标签 `ci-light`；3090 主机的 `3090-light` 等 brainspike 的工作流都改用 `ci-light` 后再去掉（决定 15.8）。
- runner 自己的流量和任务的外网流量都走 `https_proxy=127.0.0.1:<http_port>`，经 ssh 隧道到 egress 主机的代理，再出去。只有 `tunnel.direct`（localhost 和一个直连可用的包镜像）不走代理。
- hook 只读本地文件：inventory、policy、`etc/org.json`。

## 2. 新增一台机器

以新增一台像 3090a 这样的独立主机为例。所有步骤都在主机的 CI 账户下执行，不需要 sudo。

1. **准备。** 主机上要有 Python ≥ 3.11（系统自带的版本不够就用 conda 的）、开启 linger、`systemctl --user` 可用。在这台主机上测一下哪些 GitHub 域名能直连。
2. **装 release。** 从一个能访问 GitHub 的地方取得 release，在主机上运行：
   ```
   setup/install.sh --tag vX.Y.Z --expect <tag 指向的 commit sha>
   ```
   它会装到 `<data>/ci-runners/releases/vX.Y.Z/`，并把 `releases/hook` 指向它（只会往前移，回退要加 `--rollback`）。
3. **写 inventory。** 写 `<data>/ci-runners/etc/inventory.toml`（`etc/` 的权限为 700）。格式见 `setup/inventory.example.toml`：
   - `this` 为角色名；
   - `[hosts.<role>]` 填账户、路径、槽位、卡、`max_temp_c`、`cpus`、`python`；
   - `pools` 给出每个池子的 `labels`，不要用 `light`/`part` 这种裸类名；
   - 需要隧道时加 `[hosts.<role>.tunnel]`，并把密钥、`ssh_config`、`known_hosts` 放进 `etc/tunnel/`。

   再写 `policy.toml`，格式见 `setup/policy.org.example.toml`，schema 2。**`[repositories]` 的条目从一台在用主机的 `policy.toml` 原样拷过来**（例如某个仓库的 `bot_dispatch`、`classes`）：少了一条，那个仓库的部分作业（例如合并机器人触发的）在这台主机上会在 "Set up runner" 被拒，整个 run 丢掉（2026-11-12 的事故，design 决定 19.6）。注册 runner 之前，在任一 CI 账户上跑 `ci-run pool status`，确认这台主机那一行没有 `warn: policy: repository entries differ ...`。
4. **更新 deny 清单。** 运行 `python -m ci_runners.inventory deny <inventory> <policy>`，把输出的哈希合进仓库的 `tests/deny.sha256`，用一个 PR 提交，保证新主机的名字永远不会进入仓库。
5. **看计划。** 手动运行时要先导出 `CI_RUNNERS_ETC` 和 `CI_RUNNERS_PYTHON`：
   ```
   setup/plan.sh <role> > plan.txt
   ```
   只保存 stdout，不要把 stderr 混进去：`apply` 会逐字比对计划文本。逐行检查要写哪些文件、单元和 `.env`。计划还检查 CI 账户的 linger（根步骤 `loginctl enable-linger <account>`，没做时 `apply` 拒绝）和每个要启用的单元在 `default.target.wants`、`timers.target.wants` 里的链接（缺了就重新 `enable`）；stderr 的最后一行是现在有几个 runner 进程活着、哪些没有（design 决定 15.5，不进计划文本）。在共享账户上，计划不得碰我们子目录以外的东西，`ci-*` 单元除外。
6. **应用。** 运行 `setup/apply.sh <role> --plan plan.txt`。如果旧版本留下了没有记录的单元，`plan.sh` 和 `apply.sh` 都要加 `--adopt`，并逐个确认那些文件是我们的。
7. **注册 runner。** 需要 gh 临时拥有 `admin:org`，用完立即收回（§7）。所有 runner 都停着的时候才能注册：
   ```
   gh api -X POST orgs/chaobrain/actions/runners/registration-token --jq .token \
     | setup/register.sh <runner>...
   ```
   令牌只经过 stdin，不会落盘。注册完脚本会启动 runner。
8. **现场检查。**
   - runner 在 org 的 Default 组里显示 online；
   - `systemctl --user restart 'ci-runner@*'` 之后 5 分钟内全部恢复；
   - 用金丝雀仓库跑一次绿的和一次红的。

## 3. 删除一台机器

以 2026-11-06 下线 3090b 为例。

1. **确认没有任务在跑。** 看 CI 账户下有没有 `Runner.Worker` 进程或 `ci-` 部件 scope。
2. **停 runner。** 运行 `systemctl --user disable --now ci-runner@<name>.service`，一个一个停。不要用 `pkill`/`killall`。
3. **注销。** gh 临时拥有 `admin:org`：
   ```
   gh api orgs/chaobrain/actions/runners --paginate --jq '.runners[] | "\(.id) \(.name) \(.status)"'
   gh api -X DELETE orgs/chaobrain/actions/runners/<id>
   ```
   完成后收回 `admin:org`。
4. **删单元。**
   - 有 apply 记录（`state/setup.json`）的单元，用 `setup/uninstall.sh <role>` 删。它只删记录里的单元，有单元仍在运行就什么都不删。
   - 旧版本写的、没有记录的单元，先 `plan.sh <role> --adopt` 把它们收进记录，再 uninstall。
   - 如果是**专用** CI 账户，也可以逐个 `disable --now` 后手工删除 `~/.config/systemd/user/ci-*`，再 `daemon-reload`。共享账户上不要这样做。
5. **删 runner 目录。** 删掉 `actions-runner/<name>/`，里面有已作废的凭据。`etc/` 由维护者决定是否删除。
6. **清 inventory。** 从仓库的 `setup/inventory.example.toml` 删掉这台主机的段落（如果有）。`tests/deny.sha256` 里的哈希保留，删了反而可能让旧名字漏进仓库。
7. 最后确认 org 的 runner 列表里只剩预期的 runner。

## 4. 新增项目和成员

- **成员和私有仓库**：自动同步，什么都不用做。新成员、新私有仓库最多 10 分钟后可用。要暂停某个仓库，在每台主机的 `policy.toml` 里写它的条目，设 `paused = true`（见 `setup/policy.org.example.toml`）。
- **新项目**（仓库要先按 [consumers.md](consumers.md) §3 加 `.github/ci-runners.toml`）：
  1. 在要用的每台主机上写 `<data>/ci-runners/etc/projects/<project>.toml`，字段见 `ci_runners/registry.py` 的文档字符串：
     - `name` 与文件名同名；
     - `repository = "chaobrain/<repo>"`；
     - `context = "<project>/ci"`；
     - `retries = 1`；
     - `memory`；
     - `refresh = true`（可选）：让 `ci-envrefresh.timer` 每 30 分钟检查这个项目的 upstream，见下面的"环境刷新"；
     - `[hosts.<role>]` 下的 `env`、`memory_max`、`max_slots`、`variables`。
  2. 建环境。在主机的 CI 账户 shell 里：
     ```
     export $(grep -E '^(https_proxy|http_proxy|no_proxy|CI_RUNNERS_ETC|CI_RUNNERS_PYTHON)=' <data>/actions-runner/<任一 runner>/.env)
     GH_TOKEN=<只读令牌> <data>/ci-runners/releases/hook/bin/ci-run env build <project> --conda <conda 路径>
     ```
     - 令牌用 fine-grained 的，对该仓库和其私有 upstream 只给 Contents: read，只为这一条命令设置，**不要**存在主机上。
     - 一定要带上 runner 的 `no_proxy`。否则包镜像也会被送到 egress 代理，代理会返回 403。
     - 构建结果放在 `envs/<project>-<时间戳>/`，全部成功后 `envs/<project>` 才切过去，并保留前两次构建。
     - 命令会打印要写进 registry 的 `env` 和 `variables`。可以先用 `--dry-run` 看步骤。
  3. 让仓库推一个 head，看金丝雀结果。

- **共享账户的主机（h100）不放令牌**（design 决定 15.8 第 7 条）。令牌放在构建进程的环境里，同账户的其他人构建期间就能读到。改用另一台主机上这个项目的环境保存的记录来建：
  ```
  cat <3090 主机的 data>/ci-runners/envs/<project>/ci-env.json \
    | ssh <h100> '<导入 .env 的代理变量>; <data>/ci-runners/releases/hook/bin/ci-run env build <project> --kept - --conda <conda 路径>'
  ```
  记录里的规格、requirements 文件和仓库 commit 是那台主机用令牌读的；`--kept` 按刷新的规则检查它（仓库要和 registry 一致、规格能解析、upstream 和规格对得上），upstream 不带凭据按分支现在的 head clone（私有 upstream 会失败）。之后 `env refresh` 照常跟着 upstream 走。默认分支改了规格，要先在 3090 主机上 `env build`，再用它的新记录在 h100 上 `--kept` 建一次。

- **conda 表。** 规格里的 `[conda]` 表（见 [consumers.md](consumers.md) §3）和 `python` 在同一次 `conda create` 里求解，在任何 pip 步骤之前。表格式不对就什么都不建，错误写在 stderr。`--dry-run` 会打印 `conda create` 那一行，先看一眼。
- **环境刷新**（design §4.9）。`ci-run env refresh <project>` 不要令牌：它读环境里保存的 `ci-env.json`（规格、requirements 文件、每个 upstream 的 commit），用 `git ls-remote` 看分支动没动，动了才重建，建好后一次 rename 换上。有 upstream 读不了（私有，或者没有路由）就拒绝，什么都不建；任何一步失败都保留旧环境。在 v1.4 之前建的环境没有保存规格，要先用 `env build` 重建一次。`ci-run env refresh --all` 处理这台主机上 registry 里写了 `refresh = true` 的每个项目，一个失败不影响其他项目，只要有失败，退出码就是 2。同一个项目同一时刻只有一个 build 或 refresh 在跑（`envs/.<project>.lock`），撞上的刷新等下一轮。
- **部件用哪个构建。** `envs/<project>` 是会被 refresh 移动的链接。部件开始时把它解析一次，`PATH` 和 `CONDA_PREFIX` 都指向那个构建目录，部件记录（`record.json`）的 `env_build` 写下构建名；部件在 `<构建>/.inuse` 上持有共享 `flock` 直到结束（supervisor 继承该描述符，runner 停了也不放）。清理旧构建时只删能拿到独占非阻塞锁的构建，被部件占用的构建多留一轮，拿不准就保留。
- **刷新单元。** `setup/units/ci-envrefresh.{service,timer}`：timer 每 30 分钟触发，service 运行 `ci-run env refresh --all`。service 读 `etc/envrefresh.env`（可选，由维护者写，模式 600）：放 runner `.env` 里的 `https_proxy`、`http_proxy`、`no_proxy`，以及 `CI_RUNNERS_CONDA=<conda 路径>`。git 的 `ls-remote` 和 clone 都走这个代理。这两个文件随仓库发布，由 setup 计划在对应主机上安装，计划要经维护者批准。

**排空一台主机**（design 决定 13.1）。维修、重启 runner 之前，以 CI 账户身份：

```sh
ci-run drain on                 # 整台主机不再接新部件
ci-run drain on --pool cards    # 只排空一个池子（写 inventory 里的键或类名，如 gpu）
ci-run drain status             # 看状态，以及还在跑的部件 scope
ci-run drain off                # 恢复整台主机和所有池子；--pool P 只恢复那个池子
```

被排空的部件以 `unavailable`（`drained: …`）结束，`retry-on` 指向 inventory `peer_labels` 写的另一台主机的同类池子；已经开跑的部件会跑完，`claim`、`salvage`、`verdict` 不受影响。`drain status` 显示没有部件 scope 后再重启 runner。状态文件是 `state/drain.json`，重启后仍有效；文件坏了按整台主机已排空处理：只有不带 `--pool` 的 `drain off` 会重写它（同时恢复整台主机），带 `--pool` 的命令和 `drain on` 会拒绝，免得从空状态开始悄悄恢复整台主机。这只对用 `@v1.5.0` 以后版本的调用方有效。

**主机忙时让出部件**（design 决定 13.2）。`ci-hostload.timer` 每 60 秒运行 `ci-run hostload sample`，往 `state/hostload/<日期>.jsonl` 追加一行：其他账户（CI 账户以外的所有账户，维护者本人的也算）占用的 CPU 核数、内存 GiB、每张卡的显存 MiB，以及别的账户对 host lock 的独占持有。一个部件的第一次尝试在 gate 里读最近的窗口：某条规则在窗口内每个样本都达到阈值，部件就以 `unavailable`（`busy: …`）结束，去 `peer_labels` 写的另一台主机的同类池子。原因写出规则、测到的值和阈值，例如 `busy: cpu 17.2 >= 14 cores for 10 min`，可以照着调阈值。

- 默认阈值：窗口 10 分钟；CPU 为在线 CPU 数的一半（28 线程的主机是 14 核）；内存为 `MemTotal` 的一半；每张卡 2048 MiB；host lock 被同一个别的账户进程独占 20 分钟。inventory 的 `busy = { window_minutes, lock_minutes, cpu_cores, memory_gib, gpu_mib }` 可以改任一项。
- 只看 `light` 以外、有 `peer_labels` 的类的第一次尝试；重试（`attempt-of`）从不让出。同一个 head 的同一个部件只让出一次（`state/hostload/refused/` 下的标记），再来就在这台主机上排队。
- 样本不到窗口的一半（timer 停了、刚开机）就不算忙（fail open）。卡的规则只看部件能租的卡。
- `ci-run hostload status` 按部件的读法打印当前结果。reaper 删掉 2 天前的记录和 14 天前的标记。

**hook 的副本**（design 决定 13.5）。在 CI 账户有私有命名空间的主机上（inventory 写了 `local_accounts`，即两台 3090），`ci-hooks.service` 运行 `ci-run hooks copy`，把 `<data>/ci-runners/hooks/` 里的两个 hook 复制到账户私有 `/tmp` 里的 `/tmp/ci-runners-hooks/`（目录 700、文件 500），runner 的 `.env` 指向副本，所以作业日志里 "Run '<hook 路径>'" 那一行不出现数据盘路径。

- hook 靠 setup 写进去的 `base=<data>/ci-runners` 一行找 release，不靠自己的路径，所以副本跑的是同一个 release。
- runner 单元 `Requires=`/`After=ci-hooks.service`，`ExecStartPre=` 检查两个副本都是可执行文件：缺一个 runner 就起不来。作业开始时 hook 文件不在，runner 在 "Set up runner" 失败。
- reaper 每 10 分钟比对副本和源文件，缺的或不同的重写，并在日志里记一行 `hook copies: rewrote …`。
- `apply` 在启用 runner 之前 `enable --now ci-hooks.service`；hook 变了就 `reload`（重新复制）。不要 `restart` 或 `stop` 它：因为 `Requires=`，restart 会把每个 runner 一起重启，stop 会把每个 runner 一起停掉（正在跑的作业也会被杀）。
- 只能在 CI 账户的 user manager 里（systemd 单元）运行：`sudo -u` 进来的 shell 不在那个命名空间，看到的是主机的 `/tmp`，`ci-run hooks copy` 在那里写的副本 runner 看不到。

## 5. 发布流程

一次发布的六步（design §9）：

1. **功能 PR**：带单元测试、夹具和一台主机上的现场检查，合并进 `main`。
2. **stamp PR**：把 `actions/RELEASE` 改成 `vX.Y.Z`。本仓库的工作流不调用 action，所以没有 ref 要改；`tests/test_actions.py` 会检查工作流里的 ref 与 stamp 一致、文档示例引用 `@v1`。合并。
3. **打 tag**：在 stamp 的合并 commit 上打 `vX.Y.Z`。
4. **安装**：在每台主机上运行 `install.sh --tag vX.Y.Z --expect <sha>`，包括 egress 主机（以 root 身份，`CI_RUNNERS_ETC=/opt/ci-runners/etc`）。这一步不会切换任何东西。主机的计划有变化时，先看 `plan.sh`，再 `apply`。
5. **金丝雀**：金丝雀仓库的一个分支把各 action 引用为 `@vX.Y.Z`，跑一个绿的 head 和一个红的 head。
6. **移动 `v1`** 到 `vX.Y.Z`，然后开 restore PR，把 stamp 改回 `hook`。

回滚：把 `v1` 移回旧 tag 即可，旧 release 仍装在主机上。

**两个教训**（design §9，都是 2026-11-06 踩到的）：

- 金丝雀如果只引用可复用工作流 `@vX.Y.Z`，而工作流里的 action 仍是 `@v1`，跑的其实是**旧版**的主机层。所以新加的可复用工作流要让 ref 跟着 stamp 走。
- 新版改了主机文件，而旧版读不了（例如 inventory 新增了键），那么从 apply 到移动 `v1` 之间，所有任务都会失败。这种改动要么在同一个窗口里 apply 并移动 `v1`，要么先发一个能读两种格式的版本。

## 6. 网络

**egress 允许清单**（design 决定 11.10）：

- 清单在仓库的 `setup/egress/allow`，安装到 egress 主机上，是 root 拥有的 `/opt/ci-runners/etc/egress-allow`。
- 每行一个条目。`example.com` 只匹配它自己；`.example.com` 匹配它下面的所有名字，但不匹配 `example.com` 本身。只开 443 端口。
- 代理自己解析域名，只连全局地址，连接有统一的期限，连接建立之后才回 200。清单外的名字返回 403，连不上返回 502。
- 改清单的步骤：
  1. 改仓库里的 `setup/egress/allow`，提 PR，随版本发布；
  2. 在 egress 主机上以 root 把新 release 的 `setup/egress/allow` 复制到 `/opt/ci-runners/etc/egress-allow`；
  3. `systemctl restart ci-egress`。
- 查看拒绝了什么：在 egress 主机上运行 `journalctl -u ci-egress`，每个连接一行。

**egress 代理的连接上限**（design 决定 17）：

- 同时最多 `--max-connections` 个连接，默认 1024，unit 不传这个参数。unit 的 `LimitNOFILE=4096`：每个连接两个文件描述符，另加约 40 个；要调高上限，`LimitNOFILE` 至少要 2 × N + 64，否则代理启动时在 journal 里写一行 `egress: warning: the file-descriptor limit …`。
- 一端已经结束（EOF）的连接，另一个方向 60 秒没有数据就关掉，最长 300 秒；两个方向都还开着的连接（long poll、上传、下载）不受影响。
- 超过上限的连接收到 503。journal 里每分钟最多一行：`egress: refused 503: too many connections, <open> open of <cap>; <n> refused since the last such line`，`<n>` 是上一行之后被拒的个数。
- 每 5 分钟一行峰值（只在峰值变了或有拒绝时写）：`egress: peak <p> open of <cap> in the last 300 s, <r> refused at the cap`。`<p>` 接近 `<cap>` 时，在它被打满之前就要加上限（同时加 `LimitNOFILE`），或者查是哪台主机的连接变多了。
- 查看：`journalctl -u ci-egress -g 'peak|503'`。
- 发布时 egress 主机先装（§5 第 4 步）：`install.sh` 之后，以 root 把新 release 的 `setup/egress/ci-egress.service` 复制到 `/etc/systemd/system/`，然后 `systemctl daemon-reload`、`systemctl restart ci-egress`。重启会断开当前所有隧道一次，runner 会自己重连。

**主机的隧道**：

- `ci-tunnel.service` 运行 `ssh -N -L 127.0.0.1:<http_port>:127.0.0.1:<egress_port>`，连到 egress 主机上一个受限账户。
- 那个账户 `authorized_keys` 里的那一行是 `restrict,port-forwarding,permitopen="127.0.0.1:<egress_port>",permitlisten="127.0.0.1:9",command="/usr/sbin/nologin"`。要吊销，删掉这一行即可。
- 断线后 90 秒内发现，10 秒后重启。
- runner 的 `.env` 里：
  - `https_proxy`/`http_proxy` 指向这个本地端口；
  - `no_proxy` 只放 inventory 的 `tunnel.direct`。

**维护者工作站上的绕法**（工作站本身也要走代理才能连 GitHub）：

- `gh`：取消所有代理变量再运行，即 `env -u ALL_PROXY -u all_proxy -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy gh ...`。如果 `github.com/login`（设备码登录）断开，就让 gh 走代理。
- `git`：只给 github.com 配代理，即 `git -c http.https://github.com.proxy=<代理> fetch|push`。
- 主机上的 git：用 `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.proxy GIT_CONFIG_VALUE_0=$https_proxy`，不用改任何配置文件。

## 7. 内存与权限

**内存**（design 决定 11.11）：

- 在 cgroup v1、systemd 249 的主机上（当年的 h100），user scope 没有内存控制器，`MemoryMax` 不生效。
- 所以由部件的监督进程自己限内存：每 2 秒，从 systemd 层级里 scope 的 `cgroup.procs` 读出所有进程，把它们的 `RssAnon` 加起来；超过上限就杀掉 scope 里除监督进程以外的所有进程，并记一次 OOM，结果是 `oom`。
- `OOMPolicy=continue` 只在 systemd ≥ 253 时下发：249 不认识这个参数，下发了部件会立刻 `lost`。
- 项目 slice 的 `memory_max` 在这种主机上不生效。总量靠 CI 槽位来限：最多 `slots` 个部件，每个都在自己的上限之内。

**gh 的权限**：

- 日常只需要 `repo`、`read:org`、`workflow` 等，不要常驻 `admin:org`。
- 注册或注销 runner、改 runner 组时，用 `gh auth refresh -h github.com -s admin:org` 临时加上（需要在浏览器里批准设备码）。用完运行 `gh auth refresh -h github.com -r admin:org -s read:org` 收回：加 `admin:org` 时 GitHub 会把 `read:org` 并进去，只撤 `admin:org` 就会连读 org 的权限一起丢掉，工作站的快照推送随之失败，60 分钟后所有主机的任务会全部以 `stale` 被拒。
- `admin:org` 永远不放在 runner 主机上。

## 8. 排障

| 现象 | 原因与处理 |
|---|---|
| runner 掉线 | 先查 `systemctl --user status ci-tunnel`：隧道断了，runner 的流量就断。再查 egress 主机的 `ci-egress` 和 `journalctl -u ci-egress`，有 `refused 503: too many connections` 就是连接上限满了（§6）。隧道恢复后 runner 会自己重连。 |
| 所有任务在 "Set up runner" 被拒，原因 `stale` | 快照太旧。查工作站的 `systemctl --user status ci-orgsync-push.service`、gh 登录和 ssh。手动推一次：`systemctl --user start ci-orgsync-push.service`。 |
| GitHub 的定时任务停了，当晚没有 nightly | 先看 3090b 维护者账户的 `journalctl --user -u ci-nightly-backstop`：每次运行先打印 checkout 的分支和 commit，再打印 gate 的一行结论。失败多半是 gh 登录、代理或 GitHub 出错，下一次触发会再试；过了 23:00 UTC 就手动派发。 |
| ci-runners 里出现 `No runner of <host> online` | 那台主机的 runner 在 GitHub 看来全部离线，已经连续两次检查（约 15 分钟）。到那台主机的 CI 账户看 `systemctl --user status 'ci-runner@*' ci-hooks ci-tunnel` 和日志，再看 `ci-run pool status`。runner 恢复后，下一次检查会自动评论并关闭 issue。监视器自身的记录在 3090b 维护者账户的 `journalctl --user -u ci-runner-watch`。 |
| 部件一秒内就 `lost` | 多半是 `systemd-run` 拒绝了某个参数（例如在旧 systemd 上用了 `OOMPolicy`）。到部件记录 `state/parts/<repo>/<run>-<attempt>/<id>-<n>/` 里看 `log` 和 `record.json`。 |
| 报错 `unknown keys [...]` 或 inventory 无法加载 | 主机文件和正在跑的 release 版本不一致（§5 第二个教训）。确认 `releases/hook` 和 `v1` 指向的版本，再决定 apply 还是移动 `v1`。 |
| 想知道某个部件是哪个 runner 跑的 | 作业 API 的 `runner_name`（被调用的工作流里的作业也有，design §2），或者主机上部件记录 `record.json` 里的 `runner`。 |
| 同一个 head 重新触发没有跑 | claim 跳过了已经绿的 head，推一个新 commit 再跑。 |
| env build 时出现 403 | 缺少 `no_proxy`，包镜像被送到了 egress 代理。按 §4 从 runner 的 `.env` 导入代理变量。 |
| 卡一直 `unavailable: health gate` | 卡温 ≥ 80 °C 或读不到。用 `nvidia-smi` 看温度。 |

## 9. 验收记录（RUN-5，2026-11-06）

金丝雀仓库：chaobrain/ci-canary。

**已现场验证**：

- 9 个 runner 在 org 级在线，重启后能恢复；
- 任务里 `sudo -n true` 失败；
- dependabot 的 PR 被拒（`actor`）；
- 请求 `[self-hosted, light]` 的任务从不会落到 h100；
- 公开仓库的任务不会被派发（runner 组不对公开仓库开放）；
- fork PR 不可能出现（org 禁止 fork 私有仓库）；
- 快照过期时被拒（`stale`）；
- 经隧道访问 `github.com` 和 Actions 服务正常；清单外的名字返回 403；
- 端到端全绿；CPU 和 GPU 各自只在自己的部件里变红；
- `oom` 加重试；
- 停掉 runner 后部件 `lost`，并被 salvage。

**未现场验证**：

- 热停止：无法人为把卡加热，只有单元测试覆盖；
- 非成员的真人账户、新加入的成员：需要第二个账户。

**2026-11-06 退役第一条路径**（design 决定 11.12，v1.3.0）：

- 3090b 的 7 个 runner 已停止并在 org 注销，runner 目录已删除，CI 账户里的 `ci-` 单元也已全部删除；org 里只剩 h100 的 9 个 runner；
- CI 代理、`place`、主机锁、公开仓库 GPU 路径的代码都已删除；
- v1.3.0 的金丝雀在 `@v1.3.0` 上跑了一绿一红，`v1` 移动之后又在 `@v1` 上跑了一次绿。

**2026-11-07 h100 退出 CI**（design 决定 12.17）：

- h100 的 9 个 runner 已在 org 注销；工作站的快照推送不再推到 h100；
- `h100-test.yml` 已删除，它唯一的调用方是金丝雀仓库；之后只能自己组合 action（[consumers.md](consumers.md) §3）。

**2026-11-11 h100 以共享账户回来**（design 决定 15.8）：

- 22 个 runner（light 8、part 9、每卡一个 gpu 共 5）重新在 org 注册；旧 runner 目录里的凭据（包括 runner 迁移留下的 `.runner_migrated`）先挪到 `retired-20261111/`，否则 `config.sh` 认为已经配置过；
- 工作站的快照推送和 runner 监视都加回 h100；
- 三台主机的 CI 注册表互相用强制探测读对方（`probe_key` 可以是列表）；h100 上不放集群密钥，所以在 h100 上跑的 plan 看不到集群目标。

## 10. Slurm 集群（design 决定 14，RUN-9 起）

集群是 3090 主机 runner 的远程目标：`slurm` 池在 3090 主机上，部件以 `sbatch` 作业跑在集群上，集群上不常驻任何东西（没有单元、定时器、隧道或监听端口，决定 14.11）。所有命令都在一台驱动主机（3090）上以 CI 账户运行。

**站点配置**：`etc/slurm/<site>.toml`（模式 600），格式见 `setup/slurm/site.example.toml`（RUN-9b 起；只有占位符）。站点名用角色名，不用机构的主机名。写的是：GPU 申请语法（例如 `--gpus={n}`）、额外的 `sbatch` 参数、登录节点的 Python、egress 方式、预算（每日、每月）、每个目标的排队上限；价格表（每核时、每种卡的卡时、GPU 作业是否另付 CPU）；目标（分区、节点、卡型号、每节点卡数、`max_cards`、环境变体、每卡 CPU、每节点内存、`max_minutes`、`enabled`）；账户（ssh 别名、共享文件系统上的根目录、Slurm 账户、可用目标、权重、每月上限）。`python -m ci_runners.slurmsite check etc/slurm/<site>.toml` 校验它（文件名要等于 `site.name`，权限要是 600）。把站点的名字加进 deny 清单（同 §2 第 4 步）：`python -m ci_runners.slurmsite deny etc/slurm/<site>.toml` 打印分区、节点、账户、ssh 别名、路径等值的哈希；目标名和账户别名是我们自己起的角色名，不算；配置里没有、但同样不能进仓库的名字（登录节点、机构名、其他节点）用 `python -m ci_runners.inventory hash` 从 stdin 生成。保证它们不会进入仓库。

**装一个账户**（决定 14.9）：

先生成这个账户的 CI 密钥：`ssh-keygen -t ed25519 -N '' -f etc/slurm/keys/<account>`（私钥 600）。CI 密钥的 `authorized_keys` 行强制只能运行 gate，gate 不能写 release 或 `etc/`，所以 `apply`、`uninstall` 和安装之前的 `check` 要经过 `--via <ssh 别名>`：账户主人自己的登录（不受限的 key），驱动主机把要执行的代码从 stdin 送过去，登录节点上不装任何东西、不留任何进程。

1. `ci-run slurm check <site> <account> --via <别名>`：只读检查（安装之后不带 `--via`，经 CI 密钥的 gate）。ssh 免交互登录；登录节点上有 `sbatch`、`squeue`、`sacct`、`scancel`、`sinfo`、`rsync` 和 Python；Slurm ≥ 20.11；每个目标的分区可用、账户所在组被允许、association 的卡数 CPU 数节点数够、`sbatch --test-only` 接受生成的申请（GPU 语法不对会在这里报出）；根目录可写、剩余 50 GiB 以上；别的用户不能替换根目录：根目录和它下面的 `ci-runners` 是这个账户自己的目录、不是链接、组和其他人不能写，根目录上面直到 `/` 的每一级（按写的路径和解析后的路径）属于这个账户或 root，组或其他人能写的必须带 sticky 位，其中的链接也必须属于这个账户或 root（链接的主人能改它指向哪里）（否则报 finding，`apply` 也拒绝；只看权限位，GPFS、NFSv4 的 ACL 授权看不出来，要另外确认）；egress 方式的前提条件。
2. `ci-run slurm plan <site> <account> > plan.txt`：列出所有改动（目录、从驱动主机复制过去的 release、账户在集群上的配置副本、`authorized_keys` 那一行、环境构建、canary 作业及其花费），什么都不改。维护者批准；账户的**主人**把计划打印的 `authorized_keys` 那一行加进自己的账户（`restrict`，强制命令只能运行 gate）。
3. `ci-run slurm apply <site> <account> --plan plan.txt --via <别名>`：只执行批准过的计划（目录、release、`etc/account.json`、`etc/python`；计划过期就拒绝；再跑一次不改变什么）。`authorized_keys` 那一行由账户主人加，apply 不碰它。
4. `ci-run slurm env <site> <account> <project> --via <别名>`（`GH_TOKEN` 只在本次运行的环境里，只在驱动主机上用来读 spec，不会传到集群）：在登录节点上建环境（节点的 C 库版本；不在驱动主机上建，那里的 C 库更新，pip 可能选到节点加载不了的 wheel），账户各目标用到的变体里，规格没写的共用一个 `envs/<project>-<stamp>`，规格里有 `[variant.<name>]` 的各建一个 `envs/<project>-<stamp>-<variant>`（用它的 `install` 替换基础的，它的 `conda` 包加到基础的里面一起求解）；全部建成后才把每个 `envs/<project>-<variant>` 指向自己的构建（每个一次 rename；任何一个构建失败、后面的构建拿到的上游 commit 和第一个不同（构建期间分支动了，重跑即可）、或者某个链接路径不是链接，本次的构建都删掉，所有旧链接不动；作业开始时解析链接，所以运行中的作业不受之后重建的影响）。没有链接指向的旧构建每种（基础、每个变体）保留最新 3 个，其余的在不再被链接一段时间（账户最长部件时长加 1 小时）之后删除；不再有链接指向的种类（不再构建的变体）一个也不留。登录节点没有 conda 时，先离线装 Miniforge（安装包由驱动主机下载一次、sha256 固定在代码里）到 `<root>/ci-runners/conda`。egress 只在构建期间打开：`tunnel` 是 ssh 连接带一个从 `<root>/ci-runners/run` 里的 Unix socket（700 目录，socket 600）到驱动主机 egress 转发（inventory 的 `tunnel.http_port`）的反向转发，登录节点上的 relay 只接受这次构建随机生成的 `Proxy-Authorization`（通过环境变量给 pip、conda、git，不进 argv），只转发 `CONNECT`（HTTPS 隧道），其他用户的连接直接断开，同时最多 32 个连接、30 秒内发不完整个请求头就断开，构建结束后不留任何监听；`GH_TOKEN` 也不传给 ssh 和 rsync 子进程；`proxy` 用站点的代理，`direct` 不用；`push`（D4 b）还没有实现，会拒绝。上游是匿名 clone 的（私有上游会失败）。构建步骤用 `<root>/ci-runners/run` 里一个空的临时 `HOME`、不读 pip 配置文件，所以账户自己的 `~/.condarc`、`pip.conf`（比如 egress 不放行的镜像）不影响构建；部件的作业也不读账户 `~/.local` 里的包（`PYTHONNOUSERSITE=1`）和 pip 配置，`PATH` 上没有 conda。同一个根目录下一次只跑一个构建（不分项目；它们共用 conda 和它的包缓存），后来的等前一个结束；`conda create` 返回 0 却没建出 python（例如它报了损坏的包）时构建失败。`--refresh` 用驱动主机保存的上次 spec（`state/slurm/<site>/env-<account>-<project>.json`），驱动主机为每个变体记下它的构建、spec commit 和上游 commit（只建部分变体（`--variant`）时其他变体的记录不变；记录还会合入登录节点上各 `envs/<project>-<variant>` 链接实际指向的构建，所以从另一个数据目录（例如维护者的临时目录）跑也不会丢掉其他变体），只重建上游动了或者用旧 spec 建的变体（默认是账户的全部变体），全都最新才报 up to date，不需要 token。
5. `ci-run slurm canary <site> <account> --project <project> [--target T] [--import 模块]`：经 CI 密钥的 gate，每个目标一个不超过 3 分钟的作业（卡数取 `max_cards`；计划里按作业的 `--time` 8 分钟估价），在该项目该变体的环境里检查：`--import` 给的模块能导入、驱动不低于变体的下限（`cuda13` 580；没有下限的 GPU 变体直接判失败）、作业看到的卡数对（`CUDA_VISIBLE_DEVICES`、`nvidia-smi`，导入了 jax 时还有 jax）、两卡以上先跑 P2P 自检（失败就把 `max_cards` 降为 1，之后 canary 通过才恢复）、随工作区上传的标记文件能从结果里取回。每个目标的结果（状态、退出码、节点、开始延迟（sacct 的 Submit 到 Start）、用时、按价格表的花费）记在驱动主机的 `state/slurm/<site>/targets.json`。排队超过 `--wait`（默认 30 分钟）的作业会被取消；gate 一直联系不上、看不到作业结束的，到 `--wait` 加 18 分钟也取消；取消没能确认的，记录里会写明，用 `ci-run slurm status` 查。
6. **启用目标**（决定 14.10）：canary 之后再各跑一个真实的单卡部件和双卡部件（CPU 目标跑一个 CPU 部件），都通过后才把站点配置里的 `enabled` 设为 true。

**加第二个账户**：账户主人同意并加上 key 那一行，维护者在站点配置里加 `[accounts.<alias>]`，再走一遍 1–5。不需要在 GitHub 上做任何事。部件按"本月花费 ÷ 权重"最少的账户优先选择，相同时轮流（决定 14.8）。

**卸载**：`ci-run slurm uninstall <site> <account> --via <别名>`。账户还有 `ci-` 作业在跑就拒绝（`--cancel` 会先取消它们）；先在站点配置里把账户标为 retired（本月的账本记录保留，仍计入站点上限），再删掉强制运行它的 gate 的 key 行（`authorized_keys` 是链接时拒绝），最后删根目录下的 `ci-runners`。

**公开日志里的站点名字**（design 14.4）。部件在 GitHub 上显示的内容（转发的作业日志、gate 和 ssh 的错误、原因、摘要）都先经过按站点配置生成的替换：站点名和账户别名换成 `<site>`、`<account>`（别名可能就是账户主人的名字），根目录换成 `<root>`、它的上一级换成 `<home>`，Slurm 账户 `<account>`，ssh 别名和 `ssh -G` 给出的登录节点、用户名 `<host>`、`<user>`，分区 `<partition>`，节点 `<node>`（配置里的、集群 `sinfo` 列出的全部节点（经 gate 的 `nodes`，缓存 1 小时）、作业实际跑的；压缩写法如排队原因里的 `UnavailableNodes:n[19-20]` 整个替换），还有登录节点 Python、代理和额外的 `sbatch` 参数；主机和节点连同域名后缀一起替换，ProxyJump 主机也替换，先去掉 ANSI 颜色码。8 个字符以上、含非字母的值出现在哪里都替换（包括 `%2F` 编码和 repr 里 `\n` 后面的），其他值只替换整个词（纯字母的值两边也不能是 `_`，所以分区 `standard` 不会改掉 `test_standard_normal`），节点名总是整个词，不分大小写；gate 保留 Slurm 命令错误的 4000 个字符，由驱动程序先替换再截断；文本先替换再截断；等于目标名、变体名、卡型号的值，以及 deny 清单也不收的值（少于 4 个字符或常用词；节点名 2 个字符起）不换。摘要（步骤摘要、`outcome.json` 的 `slurm`、上传的 `ci-part-*` artifact）只写目标、作业号和花费，不写站点、账户和节点；执行器自己写的原因也先替换，站点配置无效时只写 `site config: invalid`，详情在记录的 `site-error.txt`。`ssh -G <别名>` 读不出配置的账户、或者拿不到集群节点列表（没有登录节点回应，也没有缓存；回应为空、有解析不了的列表或超过 4096 个节点也算拿不到，不用缓存）时，部件在提交作业之前就以 `unavailable` 结束；驱动程序遇到意外错误时部件以 `unavailable`（`driver: <类型>`）结束，traceback 只写进记录的 `driver-error.txt`。从集群取回的文件（结果、coverage、日志）不替换，见 consumers §7。驱动主机上的记录（`job.log`、`slurm.json`）保留原样。配置里没有的名字（例如作业自己打印的其他节点）不会被替换，所以测试不要打印集群的其他信息。

**花费**：`ci-run slurm status <site>` 显示各账户本月花费和正在跑的作业。`ci-run slurm cost <site> [--month YYYY-MM] [--json]` 给出每天的花费：按目标分行，CI 的 `ci-` 作业和账户的其他作业分开，最后是本月合计（同一份账本、同一张价格表；查别的月份不会覆盖本月的账本缓存）。CI 的每日花费指标就从这里读。账本来自每个账户的 `sacct`，按站点价格表计算，只是防我们自己的错误；真正的上限要请站点管理员设在 CI 账户上（design T21）。

**排障**：

- 部件 `unavailable: pending …`：目标排队太久，原因来自 `squeue`（例如 association 的节点数上限）。
- `unavailable: p2p …`：这个目标的双卡互拷读回错误数据，告诉站点管理员；它的 `max_cards` 已降为 1。
- 驱动主机宕机时作业会一直跑到 `--time`；reaper 会在 GitHub 作业消失后用 gate 的 `reap` 取消它。
- 不要在登录节点上手工 `scancel` 别的作业，只动名字以 `ci-` 开头、属于自己账户的作业。

## 11. 设备池：`pool status` 与 `pool run`（design 决定 15.1）

**注册表**：`$CI_RUNNERS_ETC/pool.toml`（默认 `~/data/ci-runners/etc/pool.toml`，权限 600），格式见 `ci_runners/pool.py` 的模块说明。它和清单一样是私有配置，不进仓库：写好后运行 `python -m ci_runners.pool deny <pool.toml>`，把新的哈希合进 `tests/deny.sha256`。每台机器一项，写明访问方式（`ssh`/`local`/`slurm`）、核数、内存、卡、环境、工作目录和 CI slot 文件。主机端（状态探测和 `pool run` 在远端的那一半）默认用系统的 `/usr/bin/python3`；它低于 3.11 时（h100 是 3.10，没有 `tomllib`），在项里写 `host_python = "<3.11 以上的 python 的绝对路径>"`（`pool add --host-python`；和 `python` 一样是私有路径）。集群写登录账户的 ssh 别名和 `ci-slurm-gate` 的路径，每个目标一行。

**查看**：`ci-run pool status` 打印一张表，`--json` 输出每行的 `HostState` 字段。主机的空闲 slot 从 `/proc/locks` 读取，不会去占用；集群的数字来自 `sinfo`，再减去本账户运行中作业所占的关联 `GrpTRES`。

另有两个可选字段，供 brainspike 按容量放部件（design 决定 15.7）：

- **主机行的 `runners`**：`{标签: 个数}`，即这台主机按标签注册的 runner 实例数。数据来自注册表旁边的清单（`<etc>/inventory.toml`，CI 账户自己的那份），按 setup 注册 runner 的同一条规则（`setup.runners`）计算。每个实例带几个标签就计几次，共享标签也算在内：有 8 个 `slurm` runner 的 3090 主机是 `<host>-slurm: 8`、`3090-slurm: 8`。这里数的是注册的实例，不是在线的（brainspike 自己减去 GitHub 上每个标签正在跑的作业）。只有注册表里的 `local` 那一项这样读；另一台主机的数字经强制探测，由对方用自己的清单算好再传回来。没有清单（比如维护者自己的注册表），或者清单读不进来时，这一行就没有这个字段。
- **集群目标行的 `jobs_free`**：按这个目标的作业规格，现在能启动几个作业。CPU 目标的规格是它的 `cpus` 个核；GPU 目标是 `cards` 张卡（`pool status` 按 1 张算，不超过 `max_cards`）加上卡数 × `cpus` 个核（注册表里 GPU 目标的 `cpus` 指每张卡的核数）；内存按 `start_now` 的同一条规则（至少 1 GiB）。分区里每个 `idle`、`mix` 节点能放 min(空闲核 // 作业核数, 空闲卡 // 作业卡数, 空闲内存 // 作业内存) 个，各节点加起来，再受 association 余量的限制（`GrpTRES` 减去本账户运行中作业占用的 cpu，GPU 目标还有 gpu）。`start_now` 为真时它大于 0，为假时它是 0。共用一个分区的目标共用同一份余量，每一行都按整份算。表格的 `start` 列显示为 `now (2)` 这样的形式。

**CI 账户的注册表**（design 决定 15.1 第 8 条）：brainspike 的 plan 作业以 runner 的 CI 账户运行 `pool status --json`，所以每台 3090 主机的 CI 账户也有一份 `pool.toml`（模式 600），写本机（`local`）、另一台 3090 主机和集群的各个目标。CI 账户在哪里都没有不受限的登录，所以：

- **集群**：项里不写 `ssh` 和 `gate`，改写站点配置的 `site` 和 `account`（`etc/slurm/<site>.toml` 里的站点名和账户别名）。`pool status` 像部件一样经账户的 CI 密钥（`etc/slurm/keys/<account>`）调用 gate 的只读动词 `capacity`：它对账户各目标的分区跑那三个查询（`sinfo`、`sacctmgr`、`squeue`），中间用 `===` 隔开。这个动词随账户的 `ci-run slurm apply` 到集群上；还没 apply 的账户，或者某个查询失败时，表里的问题是 `the gate refused capacity`（用 `ci-run slurm check <site> <account>` 看原因）。注册表要放在 `<data>/ci-runners/etc` 里：gate 的 ssh 控制连接放在 `<data>/ci-runners/state` 下。
- **另一台主机**：项里写 `probe = "forced"`。`pool status` 不发送代码，只用 `etc/pool/keys/<项名>`（私钥 600）登录，对方 CI 账户 `authorized_keys` 里这把公钥的那一行是 `restrict`，强制运行 `ci-run pool probe`：不带参数、不读 stdin，打印对方自己注册表里唯一的 `local` 机器的探测结果（JSON）。那一行由对方主机的 setup 计划写：把公钥写进对方 inventory 的 `probe_key`，`plan.sh` 列出这一行（标记 `ci-runners-pool-probe`），批准后 `apply`；删掉 `probe_key` 再 apply（`push_key` 还在时），这一行就没了。`probe_key` 不能和 `push_key` 是同一把钥匙。别名写在 CI 账户的 `~/.ssh/config` 里（`HostName`、`User`、`Port`），对方的主机密钥固定在 `known_hosts` 里。共享账户（h100）的 `~/.ssh` 不归我们：在 `etc/pool/ssh_config` 里写这些别名（`HostName`、`User`、`Port`、`UserKnownHostsFile <etc>/pool/known_hosts`、`StrictHostKeyChecking yes`），这个文件存在时强制探测用 `ssh -F` 读它（design 决定 15.8 第 4 条）。
- **本机的 runner**：CI 账户注册表里 `local` 那一项写 `runners = "<data>/actions-runner"`（私有路径，和 `drain` 一样）。`pool status` 数这个目录下有活进程（`Runner.Listener` 或它的 `run-helper.sh`）的 runner，写进行的 `runners_online` 字段（`v1.11.0` 里叫 `runners`，是个整数；决定 15.7 把这个名字给了按标签的实例数）；一个都没有时问题是 `no runner online`，brainspike 的 plan 就不往这台主机放部件。对方经强制探测读的也是这一项。`pool run` 不看这个问题（它只占 slot，不用 runner）。数的是进程，不是 GitHub 看到的在线状态：隧道断了时进程还在，照样算（design 决定 15.5）。
- 这两种项只给 `pool status` 用，`pool run` 不会选它们。每个探测有自己的上限（主机 60 秒，集群 90 秒），并行运行，所以整张表在 plan 那一步的 120 秒之内出来。
- 紧急关闭：以每个 CI 账户运行 `ci-run drain on --pool slurm`，或者把注册表移走（plan 会退回静态池并留一条说明）。

**主机的层级 `tier`**（design 决定 19.1，R5b 起）：主机项（`local` 或 `ssh`，不能是集群）可以写 `tier = N`（1 到 99，1 最先）。后面层级的主机在前面层级有空闲、在听的 runner 时，把自己同类的空闲 runner 停着（presence 的 `tier` 原因，§15），所以作业先到前面的主机；不写 `tier` 的主机既不让也不被让（和以前一样），同一层级之间也不让。每个 CI 账户的注册表（每台主机的 presence 读自己账户的）和维护者自己的注册表要写一样的值。建议的值：h100 `1`，新主机（6000pro）`2`，3090a `3`，3090b `4`（它的计时 runner 从不让）。**先装 release、`v1` 移过去，再加这个键**：旧版本的加载器不认识它，会让这个账户的 `pool status`、路由和 presence 写容量文件失败；在那之前什么都不会让，混合版本的机群和以前一样。

**行里新加的字段**（design 决定 19.4，都是可选的，容量文件仍是 schema 1）：CI 账户自己的探测（本机，和强制探测）给主机行加上 `role`（inventory 的 `this`）、`pools`（每个 runner 池一项：`pool`、`class`、池里每个 runner 都带的 `labels`、`registered`/`online`/`free`；`listening`：空闲、卡上没有租约（我们自己部件的也算）、且 listener 持有一个已建立的 TCP 连接（IPv4 或 IPv6）的 runner 数（读得到 `/proc` 时才有）；`yielded`：presence 只因 `tier` 原因停着的 runner 数；有 `slot` 表的再加 `memory_gib`、`speed`、`workers`）、`policy`（`file`：`policy.toml` 的 SHA-256 前 12 位，和 hook 行里的 `policy=` 一样；`repositories`：它的 `[repositories]` 条目的摘要）和健康门：数据目录剩余空间低于 `disk_min_gib`，或 CI 账户对共享文件的访问缺失时，问题里加 `health: ...`，这台主机就不接部件。`policy.toml` 读不进来时问题是 `policy: does not load`（hook 会拒绝所有作业）。注册表里写了 `tier` 的主机行带 `tier`。集群目标行加上 `memory_gib`、`max_minutes` 和一个不透明的 `partition`（机器名和分区名的哈希前 12 位，同分区的目标相同，分区名不出现）。维护者账户发代码过去的探测读不到 CI 账户的 inventory 和 policy，所以那样的行没有这些字段。

**策略漂移的警告**（design 决定 19.6）：`pool status` 比较各主机行的 `policy.repositories`，多数主机持有的那个值是基准（并列就没有基准）；不一样的主机在问题一栏里显示 `warn: policy: repository entries differ from N other host(s)`，`--json` 里有 `policy_drift: true` 和 `warnings`。这只是警告，不是问题：这台主机仍算在线，它的 runner 照样接共享标签的作业；但任何主机都不会让给 `[repositories]` 条目和自己不一样的主机（那些作业它可能会拒）。处理：把多数主机的 `[repositories]` 条目拷过去（每个 CI 账户的 `etc/policy.toml`），下一次 `pool status` 警告就消失。

**容量文件与 `--fast`**（design 决定 16.5，RUN-10 R4）：plan 时的路由（R5）读的数据。

- `pool status` 每跑一次就把它的行写进 `state/capacity.json`（`etc` 旁边的 `state/`，模式 600，加锁后原子改名，带 `schema`）：每台机器一回答就写它那几行，每行带 `read_at`（读到的时间）；`local` 那一项还存下原始探测结果。主机行另有 `label_runners`：每个 runner 标签注册了几个实例、几个在线（有活的 `Runner.Listener`）、几个空闲（在线且没有 `Runner.Worker`，即没在跑作业）；`/proc` 看不到时在线和空闲是 `null`。集群目标行有 `jobs_free`。
- 定时器 `ci-capacity.{service,timer}`（CI 账户的 user 单元，setup 计划里和其他定时器一起装）每 30 秒（按墙上时钟）跑一次 `ci-run pool status`，所以另一台主机的行一直缓存在文件里。集群的数字有自己的间隔：文件里集群的行满 `capacity.cluster_seconds` 秒（注册表顶层 `capacity = { cluster_seconds = 300 }`，默认 300，可设 30 到 450）才再问一次集群，登录节点是共享的；问集群走 gate 已有的 ssh 控制连接。每个账户每天约 288 次。没有 `etc/pool.toml` 的账户，单元按条件跳过，不算失败。
- 强制探测（`ci-run pool probe`）在本账户文件里的探测结果还新鲜（2 分钟内）时直接返回它（带 `age`），不跑 `nvidia-smi`；过期了才现场探测。
- `ci-run pool status --fast [--json]` 只读这些文件，2 秒内出结果（95 分位，一台主机掉线时也一样）：强制探测的主机读对方的文件；本账户自己探测的主机（`local`，或者带代码的 ssh）现场读 slot、负载和 runner，不跑 `nvidia-smi`，卡的空闲显存取自本账户文件里那一行（过期了就当没有）；集群目标直接用文件里的行。每次 ssh 都是 `ConnectTimeout=2`，整个调用最多 1.5 秒。主机的行超过 2 分钟、集群的行超过 10 分钟算掉线：问题里加 `stale (read N s ago)`，不再有空闲 slot、核和作业。`--fast` 不写文件。
- 路由用 `ci_runners.capacity.load()` 读（默认就是 `--fast` 的结果，`fast=False` 只读本账户的文件），过期规则在里面，路由自己不解析文件。过期的集群行等于“集群现在启动不了”，路由就把部件留在共享标签上。presence（R3）每个 tick 用 `pool.record_local` 重写本机那一部分。
- 新 release 装好以后，每个有注册表的 CI 账户：`setup/plan.sh` 会列出两个新单元和 `systemctl --user enable --now ... ci-capacity.timer`，lead 批准后 `apply`；用 `systemctl --user list-timers ci-capacity.timer` 确认，过半分钟看 `state/capacity.json` 的 `written_at`。

**运行**：`ci-run pool run [--cpu N] [--mem G] [--gpu 型号[:数量]] [--on 机器] -- 命令`。它把调用者的工作树（已跟踪的文件，加上未被忽略的未跟踪文件）送到一台现在就能启动的机器上运行。这台机器先从空闲主机里选，再选按价格从低到高的集群目标；都不行就在队列最短的主机上排队。命令的输出实时回传，退出码就是命令的退出码。主机上的占用就是 CI slot，和 CI part 的是同一套，所以两边不会同时拿到同一个 slot。集群上的占用就是作业本身，走同一个 gate。运行前先比对环境，不一致就拒绝运行（退出码 3）；加 `--env-mismatch warn` 可以照常运行，但会给出警告。调用者退出时，远端命令随之结束，slot 也随之释放。brainspike 用 `scripts/pool.sh` 包了一层。

## 12. nightly 的兜底定时器（design 决定 15.3）

brainspike 的 nightly 靠 GitHub 的 `schedule` 触发，merge gate 的事件唤醒也会做同样的检查。为了在没有事件的晚上 GitHub 定时也停了的情况下仍然有 nightly，3090b 上维护者本人的账户跑一个 user 定时器（不是 CI 账户：CI 账户里没有任何 GitHub 凭据，决定 12.2）。

- 单元：`setup/workstation/ci-nightly-backstop.{service,timer}`，脚本 `setup/workstation/nightly-backstop.sh [--report-only] CHECKOUT PYTHON NET_WRAP`。
- 登录：每次运行时用 `gh auth token` 读维护者的登录，只放在 gate 进程的环境变量 `GH_TOKEN` 里，不进命令行，也不写进任何文件。没有新令牌。
- 安装（由 lead 执行，定时器属于高影响操作）：
  1. 用 `setup/install.sh --commit <sha>` 把一个 release 装到维护者自己的目录下（`CI_RUNNERS_ETC` 指向那个目录下的 `ci-runners/etc`），和快照推送的 release 分开放；
  2. 把两个单元复制到 `~/.config/systemd/user/`，在 service 里填好 `@RELEASE@`、`@CHECKOUT@`、`@PYTHON@`、`@NET_WRAP@`；
  3. 先在 user manager 下演练一次（不要在登录 shell 里跑：那里的 PATH 和 keyring 和定时器的不一样）：`systemd-run --user --wait --pipe <release>/setup/workstation/nightly-backstop.sh --report-only <checkout> <python> <wrapper>`，它只做判断、打印结论，不派发；
  4. `systemctl --user daemon-reload && systemctl --user enable --now ci-nightly-backstop.timer`，再用 `systemctl --user list-timers ci-nightly-backstop.timer` 确认下一次触发的时间。
- 前提：维护者账户开了 linger；gh 的登录存在配置文件里，而不是需要会话才能解锁的 keyring；gh 在 user manager 的 PATH 上。

## 13. 主机没有在线 runner 时的提醒（design 决定 15.6）

3090b 上维护者本人的账户跑一个 user 定时器，每 15 分钟用维护者的 gh 登录读一次 org 的自托管 runner（`GET /orgs/{org}/actions/runners`），看每台主机有没有 GitHub 认为在线的 runner。要看哪些主机写在单元里，仓库里不写主机名。runner 的名字或某个标签等于主机名、或以 `<主机名>-` 开头，就算那台主机的。

- 单元：`setup/workstation/ci-runner-watch.{service,timer}`，脚本 `setup/workstation/runner-watch.sh [--report-only] STATE NET_WRAP ORG HOST...`，它在代理包装下运行 release 自己的 `ci-run runner-watch`。
- 提醒：某台主机连续两次检查（间隔不超过 40 分钟）都没有在线的 runner，就在 `<org>/ci-runners` 开一个标题固定的 issue `No runner of <host> online`；已经有同名的 open issue 就沿用它，不重复开。一次故障只开一次：故障期间有人手动关掉，监视器不会再开。之后第一次检查看到那台主机有在线的 runner，就给每个同名的 open issue 评论并关闭。
- 状态：每台主机连续离线的次数和上次检查的时间，存在单元的状态目录里（`%S/ci-runners/runner-watch.json`，systemd 255 上是 `~/.local/state`，权限 600）。离上次检查不到 5 分钟的检查（比如手动启动）不再计数。读 runner 失败时什么都不写，下一次再试；写 issue 只试一次，失败了下一次再试。
- 登录：和 §12 一样，每次运行时用 `gh auth token` 读，只放在 `ci-run` 的环境变量 `GH_TOKEN` 里。需要 `admin:org`（读 runner）和 `repo`（写 issue）。没有新令牌。
- 局限：监视器跑在 3090b 上，所以 3090b 整台宕机、或者它的网络或维护者账户出了问题时，它报告不了 3090b 自己；其他原因（CI 账户的 user manager、隧道、和 GitHub 的连接）都会报告。
- 安装（由 lead 执行，定时器属于高影响操作）：
  1. 用 `setup/install.sh --commit <sha>` 把一个 release 装到维护者自己的目录下（和 §12 的同一个也可以）；
  2. 把两个单元复制到 `~/.config/systemd/user/`，在 service 里填好 `@RELEASE@`、`@NET_WRAP@`、`@ORG@`、`@HOSTS@`（要看的主机，空格分隔）；
  3. 先在 user manager 下演练一次：`systemd-run --user --wait --pipe <release>/setup/workstation/runner-watch.sh --report-only <state 文件的绝对路径> <wrapper> <org> <host>...`，它只打印每台主机的结论和会开、会关的 issue，什么都不写；
  4. `systemctl --user daemon-reload && systemctl --user enable --now ci-runner-watch.timer`，再用 `systemctl --user list-timers ci-runner-watch.timer` 确认下一次触发的时间。
- 前提：同 §12。

## 14. 槽位：给部件池定 `slot` 表（design 16.2，RUN-10 的 R2 起）

一个部件 runner 就是一个槽位。`part` 或 `gpu` 池在 inventory 里加一张 `slot` 表，每个 runner 一个 cpuset；setup 把 runner 在表里的序号（从 0 起）写进它的 `.env`，即 `CI_RUNNER_SLOT=<i>`。部件在这样的 runner 上绑到 `cpus[i]`，内存上限是 `memory`（不超过 registry 的上限，部件自己的 `memory` 更小时用它），命令拿到 `CI_WORKERS`、`CI_SPEED` 和（设了时）`CI_ISA`，内层超时是 `timeout-minutes` × `speed`。

```toml
part  = { count = 2, labels = ["<host>-cpu"], slot = { cpus = ["4-9", "10-15"], workers = 6, memory = "48G", speed = 1.3, isa = "x86-64-v3" } }
cards = { per_card = true, class = "gpu", labels = ["<host>-gpu"], slot = { cpus = ["16-19", "20-23"], workers = 4, memory = "40G", speed = 1.3 } }
```

怎么定：

- **`cpus`**：每个 runner 一个 cpuset，互不重叠（loader 会拒绝重叠和个数不对的表）；per-card 池按 `gpus` 的顺序每张卡一个，尽量放在卡所在的 NUMA 节点上。留出 `compile_cpus`（计时部件独占）和给本地工作、light runner 用的 CPU。cpuset 必须是 runner 进程本来就能用的 CPU，否则部件以 `unavailable`（`slot: ... are not all CPUs this runner may use`）结束。
- **`workers`**：一个部件该起几个 worker。通常等于 cpuset 的 CPU 数；内存紧时减少。
- **`memory`**：`memory` × 池里的 runner 数，加上其他池的，要放得进主机内存（cgroup v1 主机上 `ci.slice` 不生效，总量就靠它们，§7）。需要大内存的部件走 `mem-big`（至少 128 GiB 的槽位，16.1）。
- **`speed`**：同一个测试在这个槽位上每个 worker 比参考主机慢几倍，参考主机是 1.0。用同一批测试在两边各跑一次、按 worker 的实际时间比算；宁大勿小，太小会让慢主机上的部件撞超时。作业的 `timeout-minutes` 要按标签里最大的 `speed` 算（16.5）。
- **`isa`**（可选）：这个槽位上部件可以用的最高指令集级别，比如有 AVX-512 的主机限到 `x86-64-v3`，让结果和同标签的其他主机一致。执行器只把它作为 `CI_ISA` 交给命令，由命令去限制自己的数值库。

和 CI 槽位文件的关系（直到 `v2`）：CPU 部件和 `compile` 部件照旧先从主机的槽位文件（inventory `slots` 和 `files`，3090 主机上是 brainspike 的，本地的 `ci_slot.sh` 也从这里拿）拿一个 CI 槽位，拿不到就排队，直到 `queue-timeout-minutes` 后 `unavailable`。槽位文件只决定“同时能跑几个”，不带 CPU；cpuset 只看 `slot` 表。所以部件拿到的槽位文件编号和它的 `CI_RUNNER_SLOT` 不必相同。GPU 部件拿的是卡的租约，不拿 CI 槽位。把 `slots` 保持为不少于部件 runner 数，本地工作和池子运行占用的槽位才会让部件排队，而不是让槽位空着；R3 的 presence 会停掉槽位被别人占着的 runner（16.4）。

上线和回退：先确认 `v1` 已经指向接受 `slot` 的版本（R1，16.3）和会读它的版本（R2），再在 inventory 加表、跑 setup 的 plan 和 apply，让各 runner 的 `.env` 带上 `CI_RUNNER_SLOT`。只有表、`.env` 里还没有 `CI_RUNNER_SLOT` 的 runner 照旧运行；`.env` 里的序号超出表的范围、表被删掉、或 per-card 池的序号和 `CI_RUNNER_CARD` 对不上，部件都以 `unavailable`（`slot: ...`）结束，不会悄悄退回旧的绑核方式。回退就是删掉表再 apply 一次。部件记录（`record.json`）的 `runner_slot` 记下序号、cpuset、`workers`、`memory`、`speed`、`isa` 和放大后的超时分钟数。

## 15. presence：按资源停启空闲的 runner（design 决定 16.4，RUN-10 R3 起）

presence 让 GitHub 的队列只交给"现在就能开工"的 runner：某个 runner 的资源暂时用不了时，它把这个空闲的 runner 停掉（`systemctl --user stop`），资源恢复后再启动。它以 CI 账户的 `ci-presence.timer` 运行，默认每 20 秒一次（`ci-run presence`，一次一个 tick），读本机状态，不用令牌；只有注册表里有 `tier` 的主机，tick 才通过强制探测读前面层级主机的行（下面的 `tier` 原因）。

- **原因**（每个 runner 单独判断）：它的卡被别人占用（卡上有不属于我们的计算进程，或不属于我们的显存达到 `busy.gpu_mib`），或卡的健康门不通过（每卡一个 runner 的主机上：温度达到 `max_temp_c` 或读不到）；没有空闲的 CI slot 给它（slot 总数减去不属于我们的进程持有的，再减去我们忙着的部件 runner 占的；剩下的 slot 先给每个池一个 runner，在线的优先、再按顺序；所以 2 个空闲 slot、某池 2 个在线 runner、另一池 1 个停着时，前者的第二个 runner 会被停一次，让后者启动，之后就稳定了）；它的 slot 的 CPU 被不属于我们的进程占着（1 秒的采样，达到这些 CPU 的一半算，降到四分之一以下才解除；cgroup v2 上不会超过整机忙时减去我们所有单元的 CPU 时间，所以我们自己的短进程不算）；主机或它的池被 drain，或主机的健康门不通过（数据盘剩余低于 `disk_min_gib`，或 CI 账户对共享文件的访问缺失，决定 15.2）；一个独占的计时部件在跑或在等：CPU 和 `compile_cpus` 重叠的 runner（有 slot 的看 slot 的 cpuset，没有 slot 的 `part` runner 看主机的 `cpus`）停。
- **`tier` 原因**（design 决定 19.2，R5b 起，只在 tick 里判断）：本机在注册表里有 `tier` 时，`part`、`gpu`、`multi` 类的空闲 runner，只要带能力标签（`ci-` 开头的和 `mem-big`）且没有别的原因，在某台前面层级的主机"提供"这些标签时就让：那台主机的行是这个 tick 用快速强制探测现读的（`pool status --fast` 那样，2 秒内连上、1.5 秒内答完），没有问题项，45 秒内读过，`[repositories]` 条目和本机的一样，并且有一个池带全这个 runner 的能力标签、至少有一个 `listening` 的 runner。让的 runner 和别的原因一样，连续 2 个空闲 tick 后停；只因 `tier` 停着的 runner，在第一个没有原因的 tick 就启动（不用等 2 个）；presence 启动过的 runner 300 秒内不再让（不来回抖；手动或 15.5 的自动恢复启动的不在此列）；`/proc` 读不到时，让着的 runner 照样在没有原因的第一个 tick 启动。读不到、超时、太旧、有问题、旧版本（没有 `listening`）、条目不一样：都不让，已经让的在这个 tick 就启动。light、计时、集群驱动和 `home` runner 从不让。卡也一样：每张卡的 runner 看前面层级有没有在听的 `ci-gpu` runner，双卡 runner 看 `ci-gpu2`。`ExecCondition=` 不看这个原因（单元启动时不联网），手动重启的 runner 会在之后的 tick 里再让。前面层级最后一个在听的 runner 被占用后，大约 55 秒内（它的 tick 20 秒、本机的 tick 20 秒、runner 启动约 10–15 秒）后面层级的 runner 就上线接作业；同时，所有前面层级都没有空闲的后面层级会一起上线。**注意**：发到后面层级主机自己主机标签上的作业（金丝雀、组合工作流），在它让着的时候会一直等；`route.retry` 的 `cool-card`、`same-host` 遇到可能在让的主机时改走共享标签（§16）。
- **"我们的"进程**按 systemd 单元和 cgroup 判断，不按 Unix 账户：在本账户 user manager（`user@<uid>.service`）下某个 `ci-` 服务或 scope 里的进程（runner、部件的 scope、定时器的服务）。cgroup v1 的主机读 `/proc/<pid>/cgroup` 里 `name=systemd` 那一行。共享账户的主机（h100）上同一个 uid 的登录会话、别人的 `systemd-run` 都不算我们的。看不到的进程不算我们的。
- **空闲**：runner 目录下没有 `Runner.Worker` 进程，也没有这个 runner 的部件 scope。连续 2 个 tick 空闲且有原因才停；停之前再看一次，这时出现了 `Runner.Worker` 就不停（日志里记一条 `kept`）。presence 停掉的、或被单元的 `ExecCondition=` 拦下的（`Result=exec-condition`），连续 2 个 tick 没有原因就启动。忙着的 runner 永远不停；没有 `ci-` 标签的 light runner（§5.1 的 `home` 池，给本机的 salvage）永远不停；主机的 light 池还没有 `ci-light`（16.1）之前，那里的每个 light runner 都算这种 runner，drain 时也不停，所以先加能力标签、再打开 presence。别人手动停掉的 runner presence 不管。
- **单元的 `ExecCondition=`**：runner 每次启动（包括 15.5 的自动恢复、开机）先跑 `ci-run presence check <runner>`，有原因时退出 77，systemd 跳过这次启动，等 presence 的 tick 再启动它。其他任何结果（出错、旧 release 没有这个命令）都让 runner 照常启动。systemd 243 以前不认识这个键，会忽略它。
- **黑洞自动 drain**：15 分钟内 `presence.infra_failures`（默认 3）次基础设施失败就 drain 整台主机（和 15.2 的缺失一样，日志里是 `drained: infra`）。算数的是：部件的 `unavailable`，但不包括 busy、drained、队列满、等待超时（没有 slot、卡或主机锁：主机忙）、类别不接受的请求（调用方的 `gpus`、`lock`）和集群部件的（那是集群的问题）；主机没有重启过的 `lost` 部件；hook 的 `stale`、`policy`、`timeout`、`error` 拒绝。上次自动 drain 或上次 `drain off` 之前的失败不算。计数在停启之后进行，计数出错只记日志，不影响停启。不会自己解除 drain：修好以后运维执行 `ci-run drain off`。
- **只在 inventory 打开的主机上生效**：主机的 inventory 写 `presence = { enabled = true }`（可选 `tick_seconds`、`infra_failures`）。每台主机都装这个定时器，但没有打开时它什么都不做。一次只打开一台主机（16.11，lead 的步骤），且 `v1` 已经指向包含 R3 的 release。

**打开之前先观察**：以 CI 账户运行 `ci-run presence --dry-run`。它按"已经打开"的情况读本机，逐个 runner 打印状态、是否空闲、原因和这个 tick 会做什么（`stop`、`start`、`keep`、`never stopped`），以及 15 分钟内的基础设施失败数。它不写状态、不动任何单元、不 drain。

**看日志**：每次停、启、`kept`、每个基础设施失败和每次自动 drain 都是 `<data>/ci-runners/logs/presence-<UTC 日期>.log` 里的一行 JSON（`runner`、`action`、`cause`，或 `infra`、`drain`），同时打到 `journalctl --user -u ci-presence.service`。reaper 14 天后删除这些文件。`state/presence.json` 里有每个 runner 的停、启次数（给 16.8 的统计）和两个 tick 的计数。

**容量文件**（16.5）：有注册表（`etc/pool.toml`）的账户，每个打开的 tick 在停、启之后用 `pool.record_local` 重写容量文件里本机那一部分（§11），所以路由看到的空闲 runner 不会比 presence 晚一个 tick 以上（`--no-block` 的停止还没完成时，这一行仍把它算作在线，下一个 tick 更正）。写不进去时日志里记一条 `"capacity": "not written"`，tick 照常进行；`ci-capacity.timer` 仍在 30 秒内重写这个文件。

**回滚**：把 inventory 改成 `presence = { enabled = false }`（或删掉这个表）。下一个 tick 就启动它停掉的和被 `ExecCondition=` 拦下的所有 runner，然后什么都不做；`check` 也总是放行。状态文件读不出来时，回滚把所有停着的 runner 都当成 presence 停的，一并启动。

**仍然存在的竞争**：presence 只能缩小、不能消除取作业和资源变化之间的竞争。停之前的最后一次查看到 systemd 把 SIGTERM 送到 runner 之间（毫秒级），GitHub 仍可能把作业派给它：作业会被重新排队，或者以 `stopped`/`lost` 结束，由 16.5 的重试行重试，不会跑两次。tick 之后到作业开始之间资源也可能变化：执行器的拒绝（drain、busy、健康门、租约）仍然生效。

## 16. 看懂一次路由（design 决定 16.5，RUN-10 R5）

可复用工作流的 plan 作业在 `ci-light` 上跑 `ci-run route`：标准输入是计划（schema 1），标准输出是路由后的计划。容量用 `capacity.load()` 读（`pool status --fast` 的行）；`--rows FILE` 读一份事先存下的 `pool status --fast --json`，用来复现某次决定。

- **输出**：`{"schema": 1, "capacity": "read" | "unreadable: <原因>", "parts": [...]}`，每个部件保留计划里的键，另加 `labels`（作业的 `runs-on`）、`timeout_minutes`（作业的 `timeout-minutes`）、`where`（`host` 或 `slurm`）、`target`（集群部件的目标，否则空）和 `why`。
- **`why` 怎么读**：`N free`：去掉本计划前面部件订掉的之后，这组标签还有 N 个空闲 runner；`free runner unknown`：有主机 `/proc` 看不到空闲数，或者某台在线主机的 runner 表没读到，这时不溢出；`0 free; <key> can start it now`：主机上没有空闲 runner 了，这个集群目标现在就能起，部件去了 `ci-slurm`；`0 free; no cluster target can start it now`（或 `no host serves ci-slurm`、`no free ci-slurm runner`、`timing never overflows`）：留在共享标签上排队；`<m> GiB > <n> GiB`：加了 `mem-big`；`slot memory not reported`：有主机行没有 `pools`，或者这个标签的池没有 `slot` 表的内存，没法判断要不要 `mem-big`。
- **退出码**：0 已路由；1 计划时的路由错误，标准错误里每行一个，先写标签，例如 `route: ci-gpu2: part gpu.1: no host serves the label (...)`（带这个标签的主机全部掉线、drained、连不上或过期），或者某个部件的内存连 `mem-big` 的 slot 都放不下；2 计划读不进来（键不对、`needs` 不认识、id 重复等），是使用方的问题。
- **容量读不到**（没有注册表、没有容量文件、行全空，或者所有主机行都过期）：每个部件都走它的共享标签，不加 `mem-big`、不溢出、不报"没有主机提供"，退出码 0，`capacity` 和标准错误里写明原因（路径只保留文件名）。这时先查 `ci-capacity.timer`（§11）。
- **"提供"的含义**：主机行在线（没有问题项）并且它的 inventory 给这个标签注册了 runner，就算提供，哪怕 presence 因为卡被占用、slot 被占用或 timing 部件在跑而暂时停掉了全部 runner。所以"没有主机提供"只说明这些主机本身不能用（掉线、drained、过期、连不上），不是忙。
- **优先主机**（design 决定 19，R5b）：路由从不写主机标签，每个计划的部件都只带能力标签；顺序由 presence 定（§15 的 `tier` 原因）：前面层级有空闲 runner 时，后面层级的空闲 runner 停着，GitHub 只能把作业交给前面层级。主机行 `pools` 里的 `yielded`（只因 `tier` 停着的 runner）算作空位：`N free` 里包含它们，所以一个突发里超过 h100 空位的部件留在共享标签上，等后面层级的 runner 在一个反应时间内上线，而不是去集群。
- **重试**（`route.retry`）：按 `outcome.hint` 的五行：`cool-card` 去首次尝试那台主机上凉的那张卡（`<host>-gpu<M>`，只限单卡部件，双卡部件按 fallback）；`same-host` 去首次尝试所在主机这一类的主机标签，集群部件回同一个目标；`more-memory` 原标签加 `mem-big`（并 `resume`），容量证明不了有主机同时提供这两个标签时，就用不带 `mem-big` 的标签加 `resume` 重试，免得排队 24 小时；`any` 原标签；fallback 用现在的容量重新路由。首次尝试所在主机有 `tier`、而一台在线的前面层级主机也提供这个部件的能力标签时（它可能正在让），`cool-card` 按 fallback、`same-host` 按 `any`，不在让着的主机上等。每个部件最多重试一次；主机标签取自首次尝试的记录（`host`、`pool_labels`、`cool_card`），不读 `retry-on`；part action 的输出 `pool-labels` 和 `cool-card`（design 19.5）就是这两个值。
