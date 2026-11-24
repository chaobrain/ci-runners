# P1 受限文件通道试点部署方案

状态：待评审，未部署。试点为 3090a 存储端与 3090b 探测端，使用合成数据。
本文把 [P0 契约](design.md#proposed-decision-internal-ci-bundle-storage-p0) 中的
候选 SSH 网关收敛为可审查方案。审批分别覆盖协议、实现及具体系统变更，
不能把 SSH 登录授权当作部署授权。本方案不表示 P1 已验收。

## 建议批准的试点范围

采用独立服务身份的受限 SSH 文件网关。SSH 公钥只认证探测客户端，文件操作
还必须携带独立签发方签名的精确授权。既有 org-sync、pool-probe 与维护者
登录密钥均不复用、不扩权。存储入口使用受保护的根文件系统专用目录，
不直接使用本次发现父路径可被普通账户改名的大盘目录。

P1 只允许两个合成仓库身份的测试对象，不能读取或写入真实 CI 数据。
runner、项目 checkout、h100、公开 fork 和 GitHub-hosted 任务均不接入。
探测客户端与 runner 的 OS 身份不同；即使运行在同一主机，runner 也不能读取
它的私钥或调用签发方。本阶段不证明真实 CI job 已经实现运行间隔离。

## 身份与文件资源

以下为逻辑名；具体账户、路径、地址、公钥和 UID 绑定在仓库外的私有部署清单。

1. **存储身份**：3090a 新建的专用非管理员系统账户，不属于 runner、sudo 或
   docker 等特权组。SSH 仅运行固定入口，不能提供普通交互 shell。
   为兼容 SSH 执行 forced command，账户具有受控 shell；不能误将 nologin
   当作 forced command 的可用执行环境。密码与其他认证途径由 SSH 策略禁用。
2. **签发身份**：3090a 上另一个不接受 SSH 登录的系统账户。签名私钥及授权
   台账仅该身份与管理员可读，网关只安装验签公钥。签发只由管理员对已经审核
   的合成测试清单调用，不开网络签发端口，不从客户端自报环境推断权限。
3. **探测身份**：3090b 新建的非登录系统账户，持有单独的 SSH 客户端私钥。
   root 管理的一次性探测服务以该身份运行；没有 timer，默认不启用自动启动。
4. **受保护入口**：存储账户 home、SSH 授权文件、程序、配置与验签公钥由
   root 拥有，存储身份不能替换。数据目录与 login home 分开；数据的 staging、
   objects、ledger 子目录由存储身份拥有，mode 0700，文件 mode 0600。
5. **客户端配置**：客户端私钥 mode 0600、父目录 mode 0700；固定主机公钥
   由维护者经可信通道核验后提供。使用严格主机密钥校验，不自动接受未知密钥，
   不转发 agent，也不读取 runner 的 SSH 配置或连接复用 socket。

检查每一级父目录的所有者、mode、ACL、链接和实际挂载；普通账户不能改名、
替换程序或关键目录。root 文件系统试点先设 1 GiB 总对象与暂存预约额度，
可用空间低于 50 GiB 时拒绝新上传。该额度是待测试的应用层预约账本限制，
不宣称已经具有文件系统硬配额。需要硬配额时，先增加管理员的独立部署评审。

## SSH 限制与程序入口

管理员拟新增仅匹配存储身份的 SSH 配置，其他账户的有效配置必须保持一致。
授权文件放在 root 管理的独立位置，只含探测身份的一把新公钥。拟议有效策略为：

```text
Match User <store-user>
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    HostbasedAuthentication no
    GSSAPIAuthentication no
    AuthorizedKeysFile <root-owned-authorized-keys>
    AuthorizedKeysCommand none
    TrustedUserCAKeys none
    ForceCommand <root-owned-gateway-launcher>
    DisableForwarding yes
    PermitTTY no
    PermitUserRC no
    MaxSessions 1
Match all
```

授权行还使用 `restrict` 与相同的 forced command。入口拒绝非空的
`SSH_ORIGINAL_COMMAND`，包括 shell、scp、sftp 与 rsync 请求；正确客户端只开
无命令的 stdio 会话。wrapper 在启动业务程序前清理不可信环境，只启动固定路径
的解释器和已审核版本；不能接受请求传入的程序、配置路径或额外命令参数。
管理员还需核对现有 AcceptEnv、PermitUserEnvironment 与系统 rc 的有效行为；
受控账户的 home 不允许客户端创建启动脚本。不能只依赖业务程序启动后的清理。

OpenSSH 的 forced command 与 `restrict` 必须配套；限制选项及 SSH 有效配置
的检查依据为 [sshd](https://man.openbsd.org/sshd) 与
[sshd_config](https://man.openbsd.org/sshd_config)。实际部署以两端安装版本
的检查结果为准，不能直接复制新版本手册中未被本机支持的选项。

修改前保存 SSH 配置元数据及摘要，使用 `sshd -t` 检查语法，再用
`sshd -T -C` 分别核对存储身份、维护者和现有 runner 的有效策略。Include 顺序
或已有 Match 可能影响结果，发现冲突即停止。通过后仅按批准计划 reload，
保留现有维护连接；不 restart、不覆盖其他 authorized_keys。

## 授权与传输协议草案

签发方使用独立 Ed25519 密钥对 canonical JSON 授权正文签名，建议采用
`ssh-keygen -Y sign/verify` 与独立 namespace `ci-storage-capability-v1`。
验签必须同时绑定固定 signer identity、allowed-signers 公钥和 namespace，
不能使用只检查结构而不认证签发方的方式。调用方式依据
[ssh-keygen](https://man.openbsd.org/ssh-keygen)，签名能力和拒绝错误签名的行为
都需要在实际安装版本验证。

授权正文绑定 schema、audience、签发 key id、随机 jti、not-before、expiry、
操作、SSH 客户端指纹、完整仓库/运行/双重尝试/part/SHA 身份及字节上限。
publish/read 还绑定精确 manifest 与 payload 摘要；list 绑定一个明确的合成
run scope。授权有效期最长 10 分钟，允许至多 30 秒时钟偏差；过期不放行。
拒绝未知签发 key、签名/namespace/客户端不匹配、越界范围和未知字段。

每个授权允许最多三次相同请求以处理断线重试。jti 台账在同一并发锁下记录
精确请求摘要、次数和已有结果；同一授权不能更换操作、身份或内容。publish
丢响应后重复提交相同对象可返回原引用，不能重复预约容量或覆盖不同内容。
P1 每个存储登录身份只允许一个由 root 安装的探测公钥，固定入口配置绑定该
公钥指纹；同时禁用其他 authorized-keys 来源和 SSH 证书认证。这样验签使用
受信任配置中的客户端指纹，不采信请求自报的指纹。轮换时配置与授权文件须
一致，否则拒绝。该绑定实现仍须在代码评审和负向试验中证明；扩展多客户端
时必须另行设计可验证的 SSH 会话身份传递。

拟议 wire framing 使用固定 magic/version、长度受限的 JSON 请求头、单独的
manifest 长度与字节、payload 长度与字节。头部最多 64 KiB，授权正文和签名
各最多 16 KiB，manifest 最多 1 MiB，payload 最多 66 MiB。每层在读取之前验证
长度，拒绝负数、溢出、重复键、多余帧和尾随数据；不接收任意服务器文件路径。
协议实现前需把完整字段与端序固定为独立契约，不能仅凭本段自行上线客户端。

P1 synthetic probe 类只验证通道和存储隔离，不被 verdict 读取。标准 part 类
仍遵守 8 MiB ZIP/每文件 1 MiB 等原有上限，不能因 probe 上限而放宽。
生产者先预约容量、暂存和校验，再原子发布不可变 manifest；读者只读取完成引用。
同身份不同摘要明确冲突。并发最多四个传输，45 秒请求空闲超时、300 秒总截止、
最多三次有界重试。试点对象预算为 1 GiB，根文件系统可用空间低于 50 GiB
时停止接收新写入；容量计入完成对象、staging 和并发预约。这个预算是待实现的
应用准入限额，不等同于文件系统 quota；日志另有限额，失败时不能吞掉磁盘满
错误。服务会话在独立身份的资源边界内运行；探测服务建议
MemoryMax 512 MiB、CPUQuota 100%、TasksMax 32、RuntimeMaxSec 300、UMask 0077。
存储侧会话建议聚合 MemoryMax 1 GiB、TasksMax 64；须实测每个会话确实进入
新身份的受限 cgroup，不能仅创建一个未承载会话的 slice，也不能把 SSH
MaxSessions 当作总并发限制。该单元的最终 UID 绑定和差异在创建账户后复核。

## 按顺序执行的管理员清单

这是执行前审核清单，尚不是当前 `setup/plan.sh` 支持的命令。

1. **冻结输入**：记录批准的协议版本、代码 commit、构件 SHA-256、账户/路径
   绑定、服务和配置文件的逐项 before/after、负责人及试验数据清单。
   只要入口程序尚未实现或构件摘要未填写，计划状态保持 NOT READY。
2. **只读 preflight**：重查账户是否存在、完整父路径权限/ACL、空间、OpenSSH
   与解释器能力、时间同步、SSH 原配置。任一名称已被占用时停止，不接管或覆盖。
3. **离线验收实现**：完成签发、验签、网关、原子存储、限额、超时、jti 重放
   与探测程序的测试及关键变异。现有 bundle 校验库仅是其中一项依赖。
4. **批准精确系统变更**：管理员签收私有清单。每条动作执行前复核 before
   摘要；需要的账户、密钥、授权文件、SSH reload、资源单元分别列出，不批量
   授予任意 sudo，不索取或把 sudo 密码放入脚本、聊天或仓库。
5. **准备身份与资源**：先创建受隔离身份、受保护目录，生成新探测密钥与新
   签发密钥，只交换所需公钥；安装 root 拥有的构件、配置与资源边界。
   此时不启动探测，不切换 CI 后端，不增加数据删除定时器。
6. **验证 SSH 并 reload**：执行前述语法/有效策略比较；确认存储身份的其他
   认证、任意命令、PTY 和转发均被拒绝，维护者及 runner 的连接仍正常。
7. **发放 synthetic grants**：按固定测试用例生成两个合成仓库与多 run/attempt
   的授权；在独立探测身份中运行一次性服务，保存退出状态及双方脱敏日志。
8. **签收或回滚**：每项验收具有预期与实际结果、manifest、摘要、时间及
   bytes 计量。任何授权越界、旧结果或假成功立即判失败，进入回滚。

## 必须全部通过的验收

- 两端直接传输 1 KiB 与 64 MiB 的可重复生成 probe 数据，比较精确字节和
  SHA-256；不经电脑中转，记录来源/目的角色、版本与耗时。
- 四路并发，两个合成仓库、多个 run、run attempt、part attempt 互不覆盖。
  同身份同摘要幂等，同身份不同摘要冲突。
- 上传中断、校验后发布前终止、发布后丢响应：半成品不可读；重试返回正确
  引用或明确失败，不从旧尝试补洞。人工终止按 PID 与开始时间核验或停止
  本试验的 scope，不使用按命令名全局杀进程。
- 错误签名、过期、错误 namespace、错误客户端、越权 read/write/list、缺失
  grant、跨仓库/run/attempt/SHA、篡改摘要与超额重放全部被拒绝。
- 任意 shell/路径、链接、scp/sftp/rsync、PTY、agent/TCP 转发和尾随协议数据
  不能绕过固定入口。runner 身份不能读探测/签发私钥，也不能替换存储父路径。
- 到期/断线总时间有界；空间低水位、四路预约与容量耗尽时明确失败，账本无
  双重计数或漏算。对象大小、实际磁盘占用与日志占用分开记录。

P1 的成功只证明受控探测身份之间的通道。进入 P3 前，真实任务必须具备来自
受信任控制器的独立授权和运行间隔离；同一 runner UID 下发短期 bearer 授权
不能当作隔离已经成立。若最终 SSH 方案无法满足此条件，返回协议评审，选择
认证内部服务及受控代理，而不是把探测凭据交给所有 job。

## 回滚和运维边界

停止本次探测服务，停止签发并撤销本次新增客户端公钥/签发 key id；按保存
摘要恢复仅本次 SSH 配置片段，经检查后 reload。涉及身份或资源单元的动作
只针对本次新增资源，先核对没有其他依赖。保留已产生对象与日志用于诊断，
不递归删除目录、不启用 TTL、不移动发布标签、不改现有 workflow 的后端。

管理员与负责人需明确试点故障响应人、容量观测人及证据保存位置；姓名与
私有路径在私有审批记录中绑定。无人签收、没有构件或缺少隔离证据均不得启动。

## 本次清单核对结果

已只读核对两端的新账户名、拟新增的程序/配置/服务路径及现有父路径。
三个拟用账户名和十一项顶层资源路径均未被占用；受保护候选父路径由 root
拥有，不可由当前 runner 账户写入，ACL 已读取留证。两端服务端 OpenSSH
版本均为 9.6p1。此结果是检查时的快照，apply 前须重新核对，不能替代管理员
对 SSH 有效配置和最终 cgroup 的验收。

私有清单已经包含账户/路径绑定、逐项 before/after、拟议 SSH Match/公钥限制
及一次性探测服务文件，均未安装。网关/签发/探测构件尚不存在，版本摘要、
主机公钥绑定、负责人与批准记录尚未填入，清单明确标为 NOT READY / NOT APPLIED。
本次未运行 `setup/plan.sh`、未创建密钥或账户、未 reload SSH、未上传测试数据。
