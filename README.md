# ci-runners

chaobrain 各项目共用的自托管 GitHub Actions runner：主机配置、执行器、pre-job hook，以及各项目调用的 action。

v2.1.2 使用一个控制节点调度多个执行节点。私有仓库在 schema 2 测试任务中设置 `gpu: true`，即可申请一张物理卡及同机 CPU 组。控制器按逻辑设备约束从独立 GPU 队列分配。计划、测试和汇总在同一次 GitHub workflow run 中完成。

组织模式实时核验私有仓库身份，并按设备顺序自动分配本地任务。新缓存模式下，计划机可先按 pytest 用例数均分任务，接单设备再按仓库的 `.github/ci-environment.yml` 复用或构建环境。白名单内的 CPU 任务还可分流到集群并使用集群侧缓存。执行器用本机配置主动注册。部署和运行记录的查看副本由 deploy output 集中到运行目录的 output/。

- [docs/consumers.md](docs/consumers.md)：使用指南，讲管线、仓库如何接入、命令约定和常见问题（中文）
- [docs/operations.md](docs/operations.md)：运维手册，讲增删机器、新项目、发布、网络、内存和排障（中文）
- [docs/design.md](docs/design.md)：契约（英文）。文档之间有出入时以它为准
- [从头部署手册](docs/v2-single-workflow-run-rebuild.md)：v2 在新设备上的首次部署，以及旧部署的停机、清理、重建和验收
- [开发过程记录](docs/v2-single-workflow-run-development.md)：CPU 集中调度与单次 workflow run 的需求、实现、修复、测试证据和待办
- [AGENTS.md](AGENTS.md)：本仓库的工作约定

本仓库不存放任何主机清单，清单只留在主机上。

新部署统一从 `./parallel/deploy.sh init --input ../ci-deploy-input` 开始，填写配置后执行 `check → plan → apply → status`。输入目录和运行目录放在源码目录外；源码不携带凭证、账本、runner 身份或测试环境。完整命令见从头部署手册。
