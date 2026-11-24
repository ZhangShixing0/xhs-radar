# 源码基线

本项目最初在私有仓库维护，基线来自项目所有者授权复用的三套本地代码及已安装的雷达 Skill。S2 交接仓库基于提交 ceae8f8a525562798e45835ce43bd6fda0f2ff0e，取用的 radar 目录在核对时无改动。中央引擎的源码位于未提交的工作树，以下摘要记录实际读取的版本。

| 来源 | 收录与适配 |
| --- | --- |
| 中央引擎及已安装 Skill | 保留两个只读采集器、状态分类、端口与 JSON 辅助、严格详情选择和数据库结构。Python 引擎仅提取包装器调用的计划、导入、当轮分析与报告，以及它们直接使用的函数。历史基线样本导入和数据集专用的验证没有移入。包装器从本仓库加载代码，从设备解析配置与数据库。 |
| S2 交接仓库 | 保留调用参数、私有计划格式、队列格式、共享占用锁、原生详情与可选逐帖协议。新增的 check-queue 离线核对原有队列。 |
| S2 研究项目 | 从 prepare_radar_detail_batch.py 移入批次准备逻辑，从 research.py 提取时间解析、快照比较与样本筛选，从 followup_provider_binding.py 仅取准备器使用的绑定摘要。新批次写入设备私有状态目录，旧回执保留原路径绑定。 |

## 分发范围

项目所有者已授权在本 GitHub 仓库公开托管所收录的源码、文档和合成示例。所选本地源码及其项目根目录未发现适用于这些文件的单独开源许可声明，本次公开也未另行指定开源许可证。第三方小红书 MCP、浏览器、运行环境及其补丁制品均由授权设备提供，其使用和分发遵循各自许可。本仓库只含调用这些组件所需的本地接口和版本标识。

## 文件来源摘要

本表 SHA-256 对应提取前的源文件原始字节。PowerShell 采集器、提供程序版本绑定和 S2 状态分类器按原字节保留。

| 来源 | 源文件 | SHA-256 |
| --- | --- | --- |
| 中央引擎 | `scripts/xhs-radar/xhs_radar.py` | 34e0f4c00872622341d7fdb79bd0550699f241db5b000a847f008033188ca314 |
| 中央引擎 | `scripts/xhs-radar/build-detail-candidates.py` | 85b03c266f724a8727d72ed6ee6b82251cb22c200bd592f42360e82079128048 |
| 中央引擎 | `scripts/xhs-radar/collect-radar-run.ps1` | a24770dc980d3b0f7783bc577e5a8f6b2db77e88fa63986a5b271fb359ab4e87 |
| 中央引擎 | `scripts/xhs-radar/collect-qualified-details.ps1` | 4b5ec7ee73fe2357d478cd0de03c79723e3ff1f76f89d9a30bb48935c32a3ee4 |
| 中央引擎 | `scripts/xhs-radar/session-lifecycle.ps1` | 1f6aa3c69fb3f9a0cd478700c7c4d1a6df29ef5993f74fff7e6e4618ab5a2b2b |
| 中央引擎 | `scripts/xhs-radar/readonly-provider-version.ps1` | 5458a3d7141b76ae6e9ccad45792eca241a51726b200364d7b8f8be680dd0d46 |
| 中央引擎 | `scripts/xhs-local/common.ps1` | e7fbdffec29f9564f552d160d70c364015f3528778cc136195a664a2898d3776 |
| 中央引擎 | `scripts/xhs-local/case-sensitive-json.ps1` | 89982f254f2bff931819e9d31ae3a9920128f188b2cd9348fb4d433b17d767a2 |
| 中央引擎 | `scripts/xhs-local/port-guard.ps1` | 8ecc751eb10f2e4d2abae2eaa8af4802372559ad0a644f0a002033de6e4de3db |
| 中央引擎 | `schemas/xhs-radar-v1.sql` | 4213b08faaaed8cc2ccd1b4f22a2908cf24f04bd9ab7d2a652cb700cecb268d6 |
| 中央引擎 | `tests/test_xhs_radar_upgrade.py` | 7bf23690546e52372b8fea4425ed9a9ba771e7b6d5798c8504727ed74bf8b941 |
| 已安装 Skill | `scripts/run-radar.ps1` | 373cfd10847168ade74836900eb392512c0d55039366cad72f00300984821434 |
| S2 交接仓库 | `radar/radar.py` | 1a82d30bc2dc278e2c388864a7da1e8e3b7be2cc395ebe451c9c9f0aeb996f18 |
| S2 交接仓库 | `radar/native_batch.py` | 1e179c1492acde6a6a4a424709d31b8d94baac50bff5217c6059b28d7db7c156 |
| S2 交接仓库 | `radar/owned_collector.py` | 015f54567dc687a01c355a374951032d7657e0b8ca7d64c9d18d28d09b2820bb |
| S2 交接仓库 | `radar/provider.py` | 0d8c6de2b87e2fbcf26226bb3d7c0d9a95a753080e6c38601d5999497bf691c0 |
| S2 交接仓库 | `radar/seat.py` | 10b6f0c3ea927f7a3f7ffe7d72e9997514229a666c2e8e1d1c29972b3e0af7c8 |
| S2 交接仓库 | `radar/test_adapter.py` | b6e748f620762cc6489bde4df7e939e2ade2ba5ba2cba812dcc8a728a7e404ff |
| S2 交接仓库 | `radar/PROVIDER.md` | 5080ca7ba5bfd9fa706bf8c7cc01c3e331e02a185233223cfb0763c33086c650 |
| S2 研究项目 | `skills/s2-viral-research/scripts/prepare_radar_detail_batch.py` | 6a70e527ac096e9496aa5da8316ca1187d524ce2886596d0c9b324d048234ade |
| S2 研究项目 | `skills/s2-viral-research/scripts/followup_provider_binding.py` | d8de6593de0a43a093d6a4220e0f01b8299dd56ec867383196f2a5507285075f |
| S2 研究项目 | `skills/s2-viral-research/patches/radar-status-classification/v2/session-lifecycle.candidate.ps1` | 1a1e88d4148e2723cd6bf6f2595043a5a17cc8a090b5693faa59bc68f1a4f4ec |
| S2 研究项目 | `xhs_ops/research.py` | e01c01da95bee7965aef68d1a74e4330099a5cd12061375676df6e70593ef5c3 |
