# 设备使用手册

在仓库根目录运行以下 PowerShell 命令。生成计划、查看状态和检查已有队列均可离线完成。搜索与详情读取需要使用原授权设备。

## 定位已有部署

| 配置 | 何时需要 |
| --- | --- |
| `XHS_RADAR_ENGINE_ROOT` | 将雷达指向含 `data/xhs-radar`、`config`、`.local-tools` 的设备项目根。依次读取进程配置、用户配置，默认使用 Windows“文档”中的“考研自动化宣传”。 |
| `S2_MUSE_RADAR_STATE` | 指向已有 S2 计划和队列的绝对路径。默认使用 `%LOCALAPPDATA%/s2-muse/radar`。复查历史任务时使用当时的目录。 |
| `S2_RESEARCH_ROOT` | 指向旧版详情批次的宿主，以便找到原有的 `.runtime/s2/<run>/<batch>`。新批次写入队列根目录下的 `native-batches`。 |
| `XHS_RADAR_PYTHON` | 可选。为 PowerShell 包装器指定兼容的 Python 可执行文件。 |
| `XHS_RADAR_WRAPPER` | 可选。S2 入口默认使用仓库内包装器，也支持显式指定已核对摘要的原设备包装器。 |

## 考研周计划

读取设备上的关键词配置，将当周的查询写入新的临时文件。

```powershell
$weekly = Join-Path $env:TEMP ("radar-week-{0}.json" -f [guid]::NewGuid())
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts/run-radar.ps1 -Action plan -PlanPath "$weekly"
```

常规考研任务沿用该脚本的 `-Action` 和 `-RunId` 参数。每次生成计划使用新的目标文件，保留旧计划供复查。

## S2 方向计划

每个方向使用独立运行号。首次用 `plan` 冻结关键词，之后用原运行号执行 `inspect` 查看已保存的计划。

```powershell
$run = "muse-demo-" + (Get-Date -Format "yyyyMMddHHmmss")
python -B radar/radar.py plan --run $run --keyword "示例主题"
python -B radar/radar.py inspect --run $run
```

## 复查已有队列

将 `$run` 设为原运行号，`$sourceRun` 设为原搜索回执中的 `run_directory`。程序读取冻结计划、公开运行清单、搜索观察和队列卡片，逐条核对计划摘要、笔记、作者、规范链接、抽样角色与当次卡片观测。

```powershell
python -B radar/radar.py check-queue --run "$run" --source-run "$sourceRun"
```

成功时返回状态、条目数和网络标记。队列为空或记录不一致时，命令以非零退出码结束。

## 准备或续跑详情批次

`$noteId` 取自已核对的队列，`$batch` 是本次详情批次名称。以下命令只在设备上准备适配文件。

```powershell
python -B radar/radar.py native-originals --run "$run" `
  --source-run "$sourceRun" --batch "$batch" --note "$noteId"
```

取得本次在线读取授权后，加 `--execute` 才会调用原生采集器。复用旧批次时保留原编号、原笔记顺序和原宿主路径。遇到登录中断、限流或验证，保留检查点，核对回执后按原批次明确续跑。设备沿用原有的占用锁和只读提供程序。

`python -B radar/radar.py --help` 列出 `search`、`resume`、`details`、`report` 等入口。可选逐帖服务使用[设备端协议](../radar/PROVIDER.md)。运行适配器测试时，设置 `S2_RESEARCH_ROOT` 且有历史批次，测试还会在临时目录解析旧回执派生的脚本。

[返回雷达首页](../README.md)
