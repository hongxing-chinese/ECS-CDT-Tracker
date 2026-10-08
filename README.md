# ECS-CDT-Tracker（阿里云 CDT 流量跟踪与 ECS 控制）

ECS-CDT-Tracker 是一段自动化的 Python 脚本，根据阿里云 CDT 流量用量控制 ECS 实例，并在每天指定的时间将运行日志发送到飞书群机器人。

## 功能

- 支持配置多个地域和 ECS 实例，每台实例独立设置 CDT 流量阈值。
- 每轮只查询一次 CDT 流量；有实例级明细时按实例 ID 统计，否则使用该实例所在地域的流量。
- 达到或超过阈值时停止运行中或启动中的实例；低于阈值时启动已停止实例，对其他异常状态提交重启请求。
- cron 按可编辑的计划执行 ECS 检查；默认每 10 分钟一次。
- 日报任务每分钟检查一次，脚本按 `FEISHU_REPORT_TIME` 指定的时间发送（默认 17:00）。
- 飞书日报只发送检查次数、实例异常和启停操作的简报；完整日志保留在本地。
- 日志按北京时间写入 `logs/ecs-cdt-tracker-YYYY-MM-DD.log`；日报使用 `state/` 中的日期标记防止重复发送。
- 支持飞书自定义机器人签名密钥。

## 环境要求

- Linux 环境并运行 cron daemon（例如 `cron` 或 `crond`）
- Python 3.9 或更新版本
- 可访问阿里云 API 和飞书 Webhook

## 安装和配置

以下步骤以项目路径 `/home/ECS-CDT-Tracker` 为例：

Ubuntu / Debian 若缺少 venv 或 cron，可先安装：

```bash
sudo apt install python3-venv cron
```

然后在项目目录创建虚拟环境并复制配置模板：

```bash
cd /home/ECS-CDT-Tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
cp config.example.json config.json
```

在 `.env` 中填写密钥和运行配置：

```text
ACCESS_KEY_ID=阿里云AccessKeyID
ACCESS_KEY_SECRET=阿里云AccessKeySecret
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的WebhookToken
FEISHU_WEBHOOK_SECRET=可选的飞书签名密钥
FEISHU_REPORT_TIME=17:00

ECS_CDT_TRACKER_CONFIG=config.json
ECS_CDT_TRACKER_LOG_DIR=logs
ECS_CDT_TRACKER_STATE_DIR=state
ALIYUN_API_TIMEOUT_SECONDS=20
```

`FEISHU_REPORT_TIME` 使用北京时间和 24 小时制 `HH:MM` 格式，例如 `09:30`。省略时默认为 `17:00`；修改 `.env` 后下次 cron 执行时就会读取新设置。

编辑 `config.json`，为每台 ECS 填入地域、实例 ID 和流量阈值：

```json
{
  "instances": [
    { "region": "cn-hongkong", "id": "i-xxxxxxxxxxxxxxxxx", "threshold_gb": 200 },
    { "region": "cn-shenzhen", "id": "i-yyyyyyyyyyyyyyyyy", "threshold_gb": 20 }
  ]
}
```

## Cron 定时任务

先确认 cron daemon 已运行，然后执行 `crontab -e`，添加下面两行：

```cron
*/10 * * * * cd /home/ECS-CDT-Tracker && /home/ECS-CDT-Tracker/venv/bin/python ecs_cdt_tracker.py run >> /home/ECS-CDT-Tracker/run.log 2>&1
* * * * * cd /home/ECS-CDT-Tracker && /home/ECS-CDT-Tracker/venv/bin/python ecs_cdt_tracker.py report >> /home/ECS-CDT-Tracker/run.log 2>&1
```

第一行默认每 10 分钟检查一次 ECS。需要调整检查频率时，修改它开头的 cron 表达式，例如 `*/5 * * * *` 表示每 5 分钟，`0 * * * *` 表示每小时。第二行每分钟运行一次轻量时间检查，只有到达 `FEISHU_REPORT_TIME` 指定的北京时间才会发送日报。

确认任务已安装：

```bash
crontab -l
```

## 手动运行和日志

```bash
cd /home/ECS-CDT-Tracker
source venv/bin/activate
python ecs_cdt_tracker.py run
python ecs_cdt_tracker.py report
```

`run.log` 保存 cron 命令输出；`logs/ecs-cdt-tracker-YYYY-MM-DD.log` 保存按北京时间整理的完整运行日志，飞书日报会从中提取简报。`state/` 保存日报发送标记。

```bash
tail -f /home/ECS-CDT-Tracker/run.log
tail -f /home/ECS-CDT-Tracker/logs/ecs-cdt-tracker-YYYY-MM-DD.log
```
