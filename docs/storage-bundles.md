# 内部 bundle 校验基础

本实现是共享存储设计的离线基础库，不启用后端、不读主机配置、不修改
workflow。P1 的真实通道与授权隔离仍待验证，不应据此宣称共享存储上线。
总体边界见 [design.md](design.md#proposed-decision-internal-ci-bundle-storage-p0)，
部署与迁移顺序见 [storage-p0.md](storage-p0.md)。

`ci_runners.bundles` 目前只处理标准 part 的 `outcome.json` 和可选
`summary.md`。计划文件、项目报告、映射索引和 salvage 修订的发布仍待后续实现。

- `Identity` 绑定 repository id/name、run id、run attempt、part id、part attempt
  和 commit SHA；数字拒绝布尔值，part attempt 沿用现有 99 次上限。
- `build_part` 接收已经读取的字节快照，生成稳定排序、固定 ZIP 时间戳的 payload、
  schema 1 manifest 和精确 `Reference`。同样的输入与元数据得到相同引用。
  本地准备完成并不表示服务器已经原子发布。
- `read_part` 校验 manifest 的精确摘要、完整身份、complete 标记、bundle 和
  每个文件的大小与摘要、UTF-8、ZIP 文件类型和条目集合，再调用原有
  `artifacts.members`。返回原有 `(outcome, summary)`，真实 red 保持 red。
- `BundleError.code` 将错误区分为 identity、format、incomplete、size、digest
  和 archive；这些均为不可重试的校验错误。所有读取只在内存中进行，拒绝时
  不产生解包目录，也不会尝试查找旧对象。

引用必须来自后续受信任的 run descriptor。摘要不能证明写入者身份；本库
不替代网关授权、原子发布、服务端路径限制或网络读取的总超时与缓冲上限。
expiry 是清理候选时间；仍被运行或映射依赖保护的精确引用可以继续读取。
本库不按当前时间擅自删除或拒绝这样的对象。

## 验证

本地定向测试：`python -m unittest tests.test_bundles tests.test_artifacts -v`。
新增 13 项契约测试；既有 artifact 读取测试仍运行。
标准 CI 的 `tests/mutants.py` 注册了 16 个对应变异，覆盖数字类型、跨身份引用、
清单上限与摘要、完成标记、JSON 重复键、文件列表重复、bundle 与文件摘要、
ZIP 类型与 NUL 名称、损坏 archive、旧尝试、确定性打包、生产端文件范围与既有解析器。

这不构成跨机、CPU/GPU、Nightly 或数据迁移验收。后续完整链路接入必须先取得
具体通道与授权证据，并保留 GitHub 默认路径及原有结果判定。
