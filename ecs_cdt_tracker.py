#!/usr/bin/env python3
"""Monitor Aliyun CDT usage and reconcile configured ECS instances."""

import argparse
import base64
from collections import Counter
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo

    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Fall back to fixed UTC+8 if system tzdata is unavailable.
    BEIJING_TZ = timezone(timedelta(hours=8), "UTC+08:00")


ROOT_DIR = Path(__file__).resolve().parent
BYTES_PER_GB = Decimal(1024**3)
RESOURCE_ID_FIELDS = ("ResourceId", "InstanceId", "ProductInstanceId", "Id")
MAX_FEISHU_TEXT_CHARS = 12000


def load_environment_file():
    env_path = configured_path("ECS_CDT_TRACKER_ENV_FILE", ROOT_DIR / ".env")
    if not env_path.exists():
        return

    for line_number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(f"{env_path}:{line_number} 不是有效的 KEY=VALUE 配置。")
        if key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


@dataclass(frozen=True)
class InstanceConfig:
    region: str
    instance_id: str
    threshold_gb: float


class AliyunAPIError(RuntimeError):
    pass


class BeijingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        value = datetime.fromtimestamp(record.created, BEIJING_TZ)
        return value.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def beijing_now():
    return datetime.now(BEIJING_TZ)


def configured_path(variable, default):
    value = os.environ.get(variable)
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT_DIR / path


def configure_logging():
    now = beijing_now()
    log_dir = configured_path("ECS_CDT_TRACKER_LOG_DIR", ROOT_DIR / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ecs-cdt-tracker-{now:%Y-%m-%d}.log"

    logger = logging.getLogger("ecs_cdt_tracker")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = BeijingFormatter("%(asctime)s +0800 %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger, log_path


def load_instances(config_path):
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"找不到实例配置文件: {config_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"实例配置不是有效 JSON: {error}") from error

    if not isinstance(document, dict) or not isinstance(document.get("instances"), list):
        raise ValueError("配置文件必须包含 instances 数组。")
    if not document["instances"]:
        raise ValueError("instances 至少需要配置一台 ECS。")

    instances = []
    seen = set()
    for index, item in enumerate(document["instances"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个实例配置必须是对象。")

        region = item.get("region")
        instance_id = item.get("id")
        if not isinstance(region, str) or not region.strip():
            raise ValueError(f"第 {index} 个实例缺少有效的 region。")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError(f"第 {index} 个实例缺少有效的 id。")

        threshold_value = item.get("threshold_gb")
        if isinstance(threshold_value, bool) or not isinstance(threshold_value, (int, float, str)):
            raise ValueError(f"第 {index} 个实例的 threshold_gb 必须是大于或等于 0 的数字。")
        try:
            threshold = float(threshold_value)
        except (TypeError, ValueError):
            raise ValueError(f"第 {index} 个实例的 threshold_gb 必须是大于或等于 0 的数字。")
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError(f"第 {index} 个实例的 threshold_gb 必须是大于或等于 0 的数字。")

        region = region.strip()
        instance_id = instance_id.strip()
        if not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError(f"第 {index} 个实例的 region 格式无效。")
        key = (region, instance_id)
        if key in seen:
            raise ValueError(f"实例配置重复: {region}/{instance_id}")
        seen.add(key)
        instances.append(InstanceConfig(region, instance_id, threshold))

    return instances


def aliyun_percent_encode(value):
    # Aliyun RPC signatures use RFC 3986 encoding, with '~' left unescaped.
    return quote(str(value), safe="~-_.")


def aliyun_signature(parameters, access_key_secret, method="POST"):
    canonical_query = "&".join(
        f"{aliyun_percent_encode(key)}={aliyun_percent_encode(value)}"
        for key, value in sorted(parameters.items())
    )
    string_to_sign = f"{method.upper()}&%2F&{aliyun_percent_encode(canonical_query)}"
    digest = hmac.new(
        f"{access_key_secret}&".encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def request_aliyun(domain, parameters):
    access_key_id = os.environ.get("ACCESS_KEY_ID")
    access_key_secret = os.environ.get("ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        raise ValueError("请设置 ACCESS_KEY_ID 和 ACCESS_KEY_SECRET 环境变量。")

    signed_parameters = {
        **parameters,
        "AccessKeyId": access_key_id,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": secrets.token_hex(16),
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    signed_parameters["Signature"] = aliyun_signature(signed_parameters, access_key_secret)
    query = "&".join(
        f"{aliyun_percent_encode(key)}={aliyun_percent_encode(value)}"
        for key, value in sorted(signed_parameters.items())
    )
    request = Request(
        f"https://{domain}/?{query}",
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )

    try:
        timeout = float(os.environ.get("ALIYUN_API_TIMEOUT_SECONDS", "20"))
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise AliyunAPIError(f"阿里云 API HTTP {error.code}: {body}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise AliyunAPIError(f"请求阿里云 API 失败: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AliyunAPIError(f"阿里云 API 返回了无效 JSON: {error}") from error

    if not isinstance(result, dict):
        raise AliyunAPIError("阿里云 API 返回格式无效。")
    if result.get("Code"):
        raise AliyunAPIError(f"阿里云 API 报错: {result['Code']} - {result.get('Message', '未知错误')}")
    return result


def traffic_details_from_response(result):
    raw_details = result.get("TrafficDetails")
    if isinstance(raw_details, list):
        return raw_details
    if isinstance(raw_details, dict) and isinstance(raw_details.get("TrafficDetail"), list):
        return raw_details["TrafficDetail"]
    raise AliyunAPIError("CDT 响应中缺少有效的 TrafficDetails。")


def get_traffic_usage():
    result = request_aliyun("cdt.aliyuncs.com", {
        "Action": "ListCdtInternetTraffic",
        "Version": "2021-08-13",
    })
    region_bytes = {}
    instance_bytes = {}

    for index, detail in enumerate(traffic_details_from_response(result), start=1):
        if not isinstance(detail, dict):
            raise AliyunAPIError(f"CDT 第 {index} 条流量明细格式无效。")

        raw_traffic = detail.get("Traffic")
        if raw_traffic is None:
            raw_traffic = detail.get("TrafficBytes", 0)
        try:
            traffic_bytes = Decimal(str(raw_traffic))
        except (InvalidOperation, ValueError):
            raise AliyunAPIError(f"CDT 第 {index} 条流量值无效。")
        if not traffic_bytes.is_finite() or traffic_bytes < 0:
            raise AliyunAPIError(f"CDT 第 {index} 条流量值无效。")

        region = str(detail.get("BusinessRegionId") or "").strip()
        if region:
            region_bytes[region] = region_bytes.get(region, Decimal(0)) + traffic_bytes

        resource_id = next(
            (str(detail[field]).strip() for field in RESOURCE_ID_FIELDS
             if detail.get(field) is not None and str(detail[field]).strip()),
            None,
        )
        if resource_id:
            instance_bytes[resource_id] = instance_bytes.get(resource_id, Decimal(0)) + traffic_bytes

    return region_bytes, instance_bytes


def instance_traffic_gb(region_bytes, instance_bytes, instance):
    if instance.instance_id in instance_bytes:
        return float(instance_bytes[instance.instance_id] / BYTES_PER_GB), "实例"
    regional_bytes = region_bytes.get(instance.region, Decimal(0))
    return float(regional_bytes / BYTES_PER_GB), "地域回退"


def get_ecs_status(region, instance_id):
    result = request_aliyun(f"ecs.{region}.aliyuncs.com", {
        "Action": "DescribeInstances",
        "Version": "2014-05-26",
        "RegionId": region,
        "InstanceIds": json.dumps([instance_id], separators=(",", ":")),
    })
    response_instances = result.get("Instances", {}).get("Instance", [])
    if isinstance(response_instances, dict):
        response_instances = [response_instances]
    if not isinstance(response_instances, list) or not response_instances:
        raise AliyunAPIError(f"未在地域 {region} 找到 ECS 实例 {instance_id}。")
    status = response_instances[0].get("Status")
    if not status:
        raise AliyunAPIError(f"无法读取 ECS 实例 {instance_id} 的状态。")
    return status


def ecs_action(action, region, instance_id):
    parameters = {
        "Action": {
            "start": "StartInstance",
            "stop": "StopInstance",
            "reboot": "RebootInstance",
        }[action],
        "Version": "2014-05-26",
        "RegionId": region,
        "InstanceId": instance_id,
    }
    if action in ("stop", "reboot"):
        parameters["ForceStop"] = "false"
    return request_aliyun(f"ecs.{region}.aliyuncs.com", parameters)


def decide_action(traffic_gb, threshold_gb, status):
    if traffic_gb >= threshold_gb:
        return "stop" if status in ("Running", "Starting") else "none"
    if status == "Stopped":
        return "start"
    if status in ("Running", "Starting", "Stopping"):
        return "none"
    return "reboot"


def reconcile_instance(logger, instance, region_bytes, instance_bytes):
    traffic_gb, source = instance_traffic_gb(region_bytes, instance_bytes, instance)
    logger.info(
        "实例 %s (%s) CDT 用量 %.2f GB / 阈值 %.2f GB（来源：%s）",
        instance.instance_id,
        instance.region,
        traffic_gb,
        instance.threshold_gb,
        source,
    )
    status = get_ecs_status(instance.region, instance.instance_id)
    action = decide_action(traffic_gb, instance.threshold_gb, status)

    if action == "none":
        logger.info("实例 %s 当前状态 %s，无需操作。", instance.instance_id, status)
        return

    ecs_action(action, instance.region, instance.instance_id)
    action_names = {"start": "启动", "stop": "停止", "reboot": "重启"}
    logger.info("实例 %s 已提交%s请求。", instance.instance_id, action_names[action])


def run_keepalive(logger):
    access_key_id = os.environ.get("ACCESS_KEY_ID")
    access_key_secret = os.environ.get("ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        raise ValueError("请在环境变量中配置 ACCESS_KEY_ID 和 ACCESS_KEY_SECRET。")

    config_path = configured_path("ECS_CDT_TRACKER_CONFIG", ROOT_DIR / "config.json")
    instances = load_instances(config_path)
    logger.info("开始本轮检查，共配置 %d 台 ECS 实例。", len(instances))

    # Fetch once and build both per-instance and per-region totals from one snapshot.
    region_bytes, instance_bytes = get_traffic_usage()
    logger.info(
        "已获取 CDT 流量明细，地域用量：%s",
        json.dumps(
            {region: round(float(value / BYTES_PER_GB), 2) for region, value in region_bytes.items()},
            ensure_ascii=False,
        ),
    )

    failures = 0
    for instance in instances:
        try:
            reconcile_instance(logger, instance, region_bytes, instance_bytes)
        except Exception:
            failures += 1
            logger.exception("检查实例 %s (%s) 失败。", instance.instance_id, instance.region)

    logger.info("本轮检查结束：实例 %d 台，失败 %d 台。", len(instances), failures)
    return 1 if failures else 0


def feishu_signature(secret, timestamp):
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def send_feishu_text(webhook_url, message, signing_secret=""):
    if not webhook_url.startswith("https://"):
        raise ValueError("FEISHU_WEBHOOK_URL 必须是 HTTPS 地址。")

    payload = {"msg_type": "text", "content": {"text": message}}
    if signing_secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_signature(signing_secret, timestamp)

    request = Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"飞书 Webhook HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"发送飞书 Webhook 失败: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"飞书 Webhook 返回无效 JSON: {error}") from error

    if not isinstance(result, dict):
        raise RuntimeError("飞书 Webhook 返回格式无效。")
    code = result.get("code", result.get("StatusCode", 0))
    if str(code) not in ("0", "None"):
        raise RuntimeError(f"飞书 Webhook 拒绝消息: {result.get('msg', result.get('StatusMessage', code))}")


def split_message(message, max_chars=MAX_FEISHU_TEXT_CHARS):
    if len(message) <= max_chars:
        return [message]
    return [message[index:index + max_chars] for index in range(0, len(message), max_chars)]


def summarize_daily_log(log_content, report_date):
    lines = log_content.splitlines()
    run_count = sum("开始本轮检查，共配置 " in line for line in lines)
    completed_runs = []
    action_counts = Counter()
    instance_failures = Counter()
    task_errors = Counter()

    for line in lines:
        completed = re.search(r"本轮检查结束：实例 (\d+) 台，失败 (\d+) 台。", line)
        if completed:
            completed_runs.append((int(completed.group(1)), int(completed.group(2))))

        action = re.search(r"实例 (\S+) 已提交(启动|停止|重启)请求。", line)
        if action:
            action_counts[action.group(2)] += 1

        instance_failure = re.search(r"检查实例 (\S+) \(([^)]+)\) 失败。", line)
        if instance_failure:
            instance_failures[(instance_failure.group(1), instance_failure.group(2))] += 1

        task_error = re.search(r"任务执行失败: (.+)$", line)
        if task_error:
            task_errors[task_error.group(1).strip()] += 1

    checked_instances = sum(total for total, _ in completed_runs)
    failed_instances = sum(failed for _, failed in completed_runs)
    task_error_count = sum(task_errors.values())
    incomplete_runs = max(0, run_count - len(completed_runs))
    has_activity = run_count or completed_runs or instance_failures or task_errors

    if not has_activity:
        return f"ECS-CDT-Tracker 每日简报（北京时间 {report_date}）\n今日暂无保活检查记录。"

    status = (
        "运行正常"
        if failed_instances == 0 and not instance_failures and not task_error_count and incomplete_runs == 0
        else "存在异常"
    )
    summary_lines = [
        f"ECS-CDT-Tracker 每日简报（北京时间 {report_date}）",
        f"检查：启动 {run_count} 轮，完成 {len(completed_runs)} 轮；实例检查 {checked_instances} 次。",
        f"异常：实例失败 {failed_instances} 次，任务错误 {task_error_count} 次，未完成 {incomplete_runs} 轮。",
        "已提交请求：启动 {start} 次，停止 {stop} 次，重启 {reboot} 次。".format(
            start=action_counts["启动"],
            stop=action_counts["停止"],
            reboot=action_counts["重启"],
        ),
        f"状态：{status}",
    ]

    if instance_failures:
        details = [
            f"{instance_id}（{region}）×{count}"
            for (instance_id, region), count in instance_failures.most_common(3)
        ]
        remaining = len(instance_failures) - len(details)
        detail_text = "；".join(details)
        if remaining > 0:
            detail_text += f"；另有 {remaining} 台"
        summary_lines.append(f"实例异常：{detail_text}")

    if task_errors:
        details = []
        for message, count in task_errors.most_common(3):
            compact_message = message[:120] + ("…" if len(message) > 120 else "")
            details.append(f"{compact_message}（{count} 次）")
        remaining = len(task_errors) - len(details)
        detail_text = "；".join(details)
        if remaining > 0:
            detail_text += f"；另有 {remaining} 类错误"
        summary_lines.append(f"任务错误：{detail_text}")

    return "\n".join(summary_lines)


def send_daily_report(logger, log_path):
    now = beijing_now()
    report_time = os.environ.get("FEISHU_REPORT_TIME", "17:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", report_time):
        logger.error("FEISHU_REPORT_TIME 格式无效，请使用北京时间 24 小时制 HH:MM（例如 17:00）。")
        return 1

    report_hour, report_minute = map(int, report_time.split(":"))
    if (now.hour, now.minute) != (report_hour, report_minute):
        logger.debug(
            "当前北京时间 %s，日报仅在每天 %s 发送，本次跳过。",
            now.strftime("%H:%M"),
            report_time,
        )
        return 0

    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.error("未配置 FEISHU_WEBHOOK_URL，无法发送每日简报。")
        return 1

    state_dir = configured_path("ECS_CDT_TRACKER_STATE_DIR", ROOT_DIR / "state")
    state_dir.mkdir(parents=True, exist_ok=True)
    sent_marker = state_dir / f"feishu-report-{now:%Y-%m-%d}.sent"
    if sent_marker.exists():
        logger.info("今日 %s 日报已发送，跳过重复通知。", report_time)
        return 0

    logger.info("开始发送北京时间 %s 的每日简报。", f"{now:%Y-%m-%d} {report_time}")
    if log_path.exists():
        log_content = log_path.read_text(encoding="utf-8").strip()
    else:
        log_content = ""

    report = summarize_daily_log(log_content, now.strftime("%Y-%m-%d"))
    chunks = split_message(report)
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    for index, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            chunk = f"[第 {index}/{len(chunks)} 部分]\n{chunk}"
        send_feishu_text(webhook_url, chunk, secret)

    sent_marker.write_text(f"sent_at={now.isoformat()}\n", encoding="utf-8")
    logger.info("飞书日报简报发送成功，共 %d 条消息。", len(chunks))
    return 0


def main():
    parser = argparse.ArgumentParser(description="ECS-CDT-Tracker：阿里云 CDT 流量跟踪与 ECS 控制")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "report"),
        default="run",
        help="run 检查并按阈值控制 ECS；report 只在 FEISHU_REPORT_TIME 指定的北京时间发送日报。",
    )
    args = parser.parse_args()

    try:
        load_environment_file()
    except Exception as error:
        print(f"读取环境变量文件失败: {error}", file=sys.stderr)
        return 1

    logger, log_path = configure_logging()

    try:
        if args.command == "report":
            return send_daily_report(logger, log_path)
        return run_keepalive(logger)
    except Exception as error:
        logger.exception("任务执行失败: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
