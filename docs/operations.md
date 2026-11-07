# 运维手册

这份手册写给维护主机和发布版本的人。契约是 [design.md](design.md)（英文），本文与之不一致时以 design.md 为准。仓库怎么接入，看 [consumers.md](consumers.md)。

本仓库公开，文档里**不写任何清单信息**：主机名、ssh 别名、IP、账户名、home 路径、令牌都不出现，只用角色名（design §3.3），由 `tests/test_no_inventory.py` 把关。下文的 `<data>` 指主机上 CI 账户数据目录的根（inventory 的 `data`）。

## 1. 拓扑

| 角色 | 跑什么 | 以谁的身份 |
|---|---|---|
| **3090b**、**3090a** | 各自的 runner 池（`<host>-light`、`-cpu`、`-gpu<N>`、`-multi`，3090b 另有 `-compile`，3090a 另有 `-nightly`，design §5.1）；`ci-tunnel.service`（到 egress 的 ssh 转发）；`ci-reaper.timer`、`ci-envrefresh.timer` | 专用 CI 账户的 systemd user 单元，文件都在 `<data>` 下 |
| **egress 主机** | `ci-egress.service`：HTTP CONNECT 代理，只放行允许清单里的域名的 443 端口 | root 安装，DynamicUser 运行 |
| **工作站** | `ci-orgsync-push.timer`：每 10 分钟用维护者的 gh 登录拉 org 快照，经 ssh 推到每台主机 | 维护者本人的 user 单元 |

数据流：

- GitHub 把任务派给主机的 runner。（h100 已于 2026-11-07 退出 CI，design 决定 12.17。）
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

   再写 `policy.toml`，格式见 `setup/policy.org.example.toml`，schema 2。
4. **更新 deny 清单。** 运行 `python -m ci_runners.inventory deny <inventory> <policy>`，把输出的哈希合进仓库的 `tests/deny.sha256`，用一个 PR 提交，保证新主机的名字永远不会进入仓库。
5. **看计划。** 手动运行时要先导出 `CI_RUNNERS_ETC` 和 `CI_RUNNERS_PYTHON`：
   ```
   setup/plan.sh <role> > plan.txt
   ```
   只保存 stdout，不要把 stderr 混进去：`apply` 会逐字比对计划文本。逐行检查要写哪些文件、单元和 `.env`。在共享账户上，计划不得碰我们子目录以外的东西，`ci-*` 单元除外。
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
| runner 掉线 | 先查 `systemctl --user status ci-tunnel`：隧道断了，runner 的流量就断。再查 egress 主机的 `ci-egress` 和 `journalctl -u ci-egress`。隧道恢复后 runner 会自己重连。 |
| 所有任务在 "Set up runner" 被拒，原因 `stale` | 快照太旧。查工作站的 `systemctl --user status ci-orgsync-push.service`、gh 登录和 ssh。手动推一次：`systemctl --user start ci-orgsync-push.service`。 |
| 部件一秒内就 `lost` | 多半是 `systemd-run` 拒绝了某个参数（例如在旧 systemd 上用了 `OOMPolicy`）。到部件记录 `state/parts/<repo>/<run>-<attempt>/<id>-<n>/` 里看 `log` 和 `record.json`。 |
| 报错 `unknown keys [...]` 或 inventory 无法加载 | 主机文件和正在跑的 release 版本不一致（§5 第二个教训）。确认 `releases/hook` 和 `v1` 指向的版本，再决定 apply 还是移动 `v1`。 |
| 想知道某个部件是哪个 runner 跑的 | 被调用的工作流里，API 返回的作业 `runnerName` 是空的。看主机上部件记录 `record.json` 里的 `runner`。 |
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
