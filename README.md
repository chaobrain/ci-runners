# ci-runners

chaobrain 各项目共用的自托管 GitHub Actions runner：主机配置、执行器、pre-job hook，以及各项目调用的 action。

org 里每个私有仓库都能在自己的工作流里组合这些 action，在 3090b 和 3090a 上跑 CPU、GPU、双卡等测试。成员和私有仓库的名单从 org 自动同步。

- [docs/layout.md](docs/layout.md)：源码目录与模块职责，供开发和结构调整查阅
- [docs/consumers.md](docs/consumers.md)：使用指南，讲管线、仓库如何接入、命令约定和常见问题（中文）
- [docs/operations.md](docs/operations.md)：运维手册，讲增删机器、新项目、发布、网络、内存和排障（中文）
- [docs/design.md](docs/design.md)：契约（英文）。文档之间有出入时以它为准
- [AGENTS.md](AGENTS.md)：本仓库的工作约定

本仓库不存放任何主机清单，清单只留在主机上。
