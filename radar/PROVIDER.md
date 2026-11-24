# 固定队列原帖读取的设备端协议

`read-original` 调用设备管理员明确绑定的只读程序。程序应使用默认雷达已获授权的会话和共享浏览器占用管理，一次处理一个笔记 ID，在来源页上核对同一作者。图片按设备已有证据索引优先复用。这里实现了调用端和离线协议测试；需要使用该能力的设备另行安装并实测生产服务。

| 本机变量 | 用途 |
|---|---|
| `S2_MUSE_ORIGINAL_PROVIDER` | 一个 JSON 字符串数组，第一项是本机可信程序绝对路径，其他项为固定参数。由本机管理员绑定，任务与笔记均不能指定执行程序。 |
| `S2_MUSE_RADAR_MEDIA_ROOT` | 已存在的私有图片目录绝对路径。媒体清单只接受这个目录下的相对路径和 SHA-256。 |

命令使用标准输入、标准输出传输 UTF-8 JSON，一次请求和一次结果。服务自身负责保护会话、与其他雷达对话串行、持久化已获取页和下一次允许采集的时刻。原始网络响应和会话字段不进入交接项目。

1. **离线探测。** 请求为 `{"protocol":"s2-original/1","action":"capabilities","network_allowed":false}`。结果须回传相同协议、`network_request_made:false`，以及 `capabilities` 对象中五个为真的能力：`default_radar_session`、`read_only`、`exact_note`、`local_image_files`、`shared_session_lease`。探测证明接口声明匹配，账号状态仍待在线核验。
2. **读取。** 请求含相同协议、`action=read_original`、`run_id`、`note_id`、`author_id`、`canonical_url`、`media_root`、`max_notes=1`、`reuse_existing=true`、`allow_video=false`。
3. **取回成功。** 结果含协议、`status=captured`、同一 ID 和作者、规范链接、`title`、`body`、`media_type=photo`、带时区的 `published_at` 和 `observed_at`、`metrics.likes_raw` 与 `metrics.saves_raw`。原文位置和原始字段应来自详情；空值保持空值。`expected_pages` 来自原帖，`pages` 中每一项有连续的 `index`、相对文件名 `file` 和 `sha256`。客户端核对页数、顺序、文件指纹和身份，在私有运行目录的 `originals/<ID>.json` 保存记录。每张图片的语义判断另行完成。
4. **中断。** 结果含协议及下列状态之一：`unread`、`partial`、`authentication_required`、`session_unknown`、`rate_limited`、`verification_required`、`unavailable`、`timed_out`。设备端保存已经成功的页。下一次运行会先复核已有合格清单；未闭合的调用保留尝试记录，待条件恢复并按原任务授权明确续跑时使用 `--retry`。若宿主崩溃留下 `.lock`，先核实原进程已结束及设备检查点，再清除此单篇占用标记。
