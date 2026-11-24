# 小红书雷达

S2 和考研项目可以从同一台授权设备读取公开笔记，共用一套取样历史。仓库包含计划、只读采集、固定原帖队列、SQLite 导入和报告程序。日常离线操作只需 Python 标准库。

## 解决多项目重复取样问题

搜索任务各有自己的冻结计划和运行号。完成的搜索按雷达层导入设备已有的数据库，以平台和笔记 ID 识别同一篇原帖，以运行号保留不同批次的快照。S2 可以从指定搜索的卡片中固定普通样本或边界样本。考研项目继续使用设备上的关键词配置、收藏门槛和跨作者规则。

## 快速开始

从克隆目录执行。设备状态入口使用 Windows PowerShell 5.1 和 Python 3.10+。独立的 Python 计划与队列检查可直接在有相应本地文件的环境运行。

```powershell
python -B radar/radar.py status
$plan = Join-Path ([IO.Path]::GetTempPath()) ("radar-demo-" + [guid]::NewGuid() + ".json")
python -B scripts/xhs-radar/xhs_radar.py plan --config config/keywords.example.json --plan "$plan"
```

示例配置生成两条操作，回执包含输出路径、操作数和 `network_request_made: false`。每次使用新的输出文件名，以便保留先前的计划。状态中的 `authentication_state` 为 `unknown`，`central_database_present` 表示本机数据库文件是否存在。

## 使用设备上的计划和队列

| 设备配置 | 作用 |
| --- | --- |
| `XHS_RADAR_ENGINE_ROOT` | 现有部署的项目根，包含 `data/xhs-radar`、`config`、`.local-tools`。先取进程配置，再取用户配置，默认解析 Windows“文档”中的“考研自动化宣传”。 |
| `S2_MUSE_RADAR_STATE` | S2 已有的私有计划与队列根目录，使用绝对路径。默认位置为 `%LOCALAPPDATA%/s2-muse/radar`。历史任务如指定了其他位置，应指回原目录。 |
| `S2_RESEARCH_ROOT` | 复用旧版详情批次时，定位宿主原有的 `.runtime/s2/<run>/<batch>`。新批次使用队列根下的 `native-batches`。 |
| `XHS_RADAR_PYTHON` | 可选，给 PowerShell 包装器指定兼容的 Python 可执行文件。 |
| `XHS_RADAR_WRAPPER` | 可选，S2 风格入口可显式调用已核对摘要的原设备包装器。默认使用仓库内包装器。 |

考研方向按设备现有配置生成周计划，下面的命令将结果写入临时文件。

```powershell
$weekly = Join-Path ([IO.Path]::GetTempPath()) ("radar-week-" + [guid]::NewGuid() + ".json")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-radar.ps1 -Action plan -PlanPath "$weekly"
```

S2 新建方向时执行下面两条命令。重复查看沿用原运行号。

```powershell
$run = "muse-demo-" + (Get-Date -Format "yyyyMMddHHmmss")
python -B radar/radar.py plan --run $run --keyword "示例主题"
python -B radar/radar.py inspect --run $run
```

检查**已有队列**时，将 `$run` 和 `$sourceRun` 设置为原运行号和搜索回执中的 `run_directory`。检查会读取原计划、公开 manifest、搜索观察和队列卡片，核对计划摘要、笔记及作者、规范链接、抽样角色和冻结的卡片观测。输出只含状态、条目数和网络标记。队列为空或记录不一致时返回非零退出码。

```powershell
python -B radar/radar.py check-queue --run "$run" --source-run "$sourceRun"
```

使用当前仓库提供的 Python 原帖入口准备单批时，可用下列命令。`$noteId` 来自上述队列，`$batch` 是本次详情批次的独立名称。缺省只准备设备私有适配文件。复用旧批次时使用原编号、原顺序和原宿主路径。

```powershell
python -B radar/radar.py native-originals --run "$run" --source-run "$sourceRun" --batch "$batch" --note "$noteId"
```

`python -B radar/radar.py --help` 列出 S2 的 `search`、`resume`、`details` 和 `report` 等入口。设备共用原有的占用锁和只读提供程序，在线动作需要当次授权。原生固定队列详情需要另加 `--execute`。遇到登录、限流或验证中断，保留检查点，之后按原批次明确续跑。常规考研任务可从仓库的 `scripts/run-radar.ps1` 使用原有的 `-Action` 和 `-RunId` 参数。可选逐帖服务的设备协议见 [PROVIDER.md](radar/PROVIDER.md)。

## 测试与验证范围

```powershell
python -B -m unittest discover -s tests -v
python -B radar/test_adapter.py -v
```

合成用例覆盖跨项目导入和重复运行、报告计数、详情检查点、占用处理、固定队列来源和新批次准备。设置 `S2_RESEARCH_ROOT` 且存在历史批次时，还会在临时目录解析从旧回执派生的脚本。测试使用临时数据库和本地模拟程序。

本次在授权设备上进行了离线状态、周计划及已有固定队列核对。账号当前登录、线上搜索和详情返回均未实测。

## 隐私与源码来源

设备保存 SQLite、关键词实配、运行历史、私有查找缓存和原媒体。发布清单只收录列名的源文件、SQL、合成配置、测试与文档。克隆代码后仍需连接已部署的设备才能在线运行。仓库不包含第三方提供程序的源码、补丁包或二进制。来源版本、提取范围和许可边界见 [SOURCES.md](SOURCES.md)。
