#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path("/opt/server_guard")
DEFAULT_CONFIG = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state" / "state.json"
LOG_FILE = BASE_DIR / "logs" / "server_guard.log"

STOP = False


# ============================================================
# 信号处理
# ============================================================

def on_signal(signum, frame):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)


# ============================================================
# 日志
# ============================================================

def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("server_guard")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )

        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )

        logger.addHandler(handler)

    return logger


LOGGER = setup_logging()


# ============================================================
# 基础函数
# ============================================================

def load_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")

    return data


def atomic_write_json(path, data):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

        f.flush()
        os.fsync(f.fileno())

    os.replace(
        tmp,
        path,
    )


def now_text():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def clamp_text(text, limit):
    text = text or ""

    if len(text) <= limit:
        return text

    return (
        text[:limit]
        + "\n...[已截断]"
    )


# ============================================================
# HTTP
# ============================================================

def http_post_form(
    url,
    data,
    timeout=5.0,
    verify_tls=True,
):

    body = urlencode(
        data
    ).encode("utf-8")

    req = Request(
        url,
        data=body,
        method="POST",
    )

    req.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded",
    )

    context = None

    if (
        url.lower().startswith("https://")
        and not verify_tls
    ):
        context = ssl._create_unverified_context()

    with urlopen(
        req,
        timeout=timeout,
        context=context,
    ) as resp:

        raw = resp.read(
            1024 * 1024
        )

    return json.loads(
        raw.decode(
            "utf-8",
            errors="replace",
        )
    )


def http_post_json(
    url,
    payload,
    headers,
    timeout=20.0,
):

    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")

    req = Request(
        url,
        data=body,
        method="POST",
    )

    req.add_header(
        "Content-Type",
        "application/json",
    )

    for k, v in headers.items():
        req.add_header(
            k,
            v,
        )

    with urlopen(
        req,
        timeout=timeout,
    ) as resp:

        raw = resp.read(
            2 * 1024 * 1024
        )

    return json.loads(
        raw.decode(
            "utf-8",
            errors="replace",
        )
    )


# ============================================================
# 宝塔 API
# ============================================================

def bt_api_call(
    server,
    action="GetNetWork",
):

    api_key = str(
        server["bt_api_key"]
    )

    request_time = str(
        int(time.time())
    )

    md5_key = hashlib.md5(
        api_key.encode("utf-8")
    ).hexdigest()

    request_token = hashlib.md5(
        (
            request_time
            + md5_key
        ).encode("utf-8")
    ).hexdigest()

    result = http_post_form(
        str(server["bt_url"]).rstrip("/")
        + "/system",

        {
            "request_time": request_time,
            "request_token": request_token,
            "action": action,
        },

        timeout=float(
            server.get(
                "api_timeout",
                5,
            )
        ),

        verify_tls=bool(
            server.get(
                "verify_tls",
                False,
            )
        ),
    )

    if not isinstance(
        result,
        dict,
    ):
        raise RuntimeError(
            "宝塔 API 返回非 JSON 对象"
        )

    return result


def parse_bt_metrics(data):

    # CPU
    cpu_raw = data.get("cpu")

    if (
        isinstance(
            cpu_raw,
            (list, tuple),
        )
        and cpu_raw
    ):
        cpu = float(
            cpu_raw[0]
        )

    elif "cpuRealUsed" in data:
        cpu = float(
            data.get(
                "cpuRealUsed"
            )
            or 0
        )

    else:
        raise RuntimeError(
            "API返回缺少CPU字段: "
            + str(data)[:240]
        )

    # 内存
    mem_total = 0.0
    mem_used = 0.0

    mem = data.get("mem")

    if isinstance(
        mem,
        dict,
    ):

        mem_total = float(
            mem.get("memTotal")
            or 0
        )

        mem_used = float(
            mem.get("memRealUsed")
            or 0
        )

    else:

        mem_total = float(
            data.get("memTotal")
            or 0
        )

        mem_used = float(
            data.get("memRealUsed")
            or 0
        )

    if mem_total:
        mem_pct = (
            mem_used
            / mem_total
            * 100
        )
    else:
        mem_pct = 0.0

    # Load
    load1 = 0.0
    load5 = 0.0
    load15 = 0.0

    load = data.get("load")

    if isinstance(
        load,
        dict,
    ):

        load1 = float(
            load.get("one")
            or 0
        )

        load5 = float(
            load.get("five")
            or 0
        )

        load15 = float(
            load.get("fifteen")
            or 0
        )

    return {
        "cpu": round(
            cpu,
            2,
        ),

        "mem_pct": round(
            mem_pct,
            2,
        ),

        "load1": round(
            load1,
            2,
        ),

        "load5": round(
            load5,
            2,
        ),

        "load15": round(
            load15,
            2,
        ),
    }


# ============================================================
# 状态
# ============================================================

DEFAULT_SERVER_STATE = {

    "high_count": 0,

    "recover_count": 0,

    "incident_active": False,

    "critical_sent": False,

    "incident_started_at": 0,

    "last_poll_ok": 0,

    "api_fail_count": 0,

    "api_alert_sent": False,

    "last_metrics": {},

    "pending_incident_message": "",

    "last_service_check": 0,

    "service_fail_count": 0,

    "service_alert_sent": False,

    "service_status": {},
}


def load_state(
    server_names,
):

    state = {
        "servers": {}
    }

    if STATE_FILE.exists():

        try:
            state = load_json(
                STATE_FILE
            )

        except Exception as e:

            LOGGER.error(
                "读取状态文件失败，将使用空状态: %s",
                e,
            )

    servers_state = state.setdefault(
        "servers",
        {},
    )

    for name in server_names:

        merged = deepcopy(
            DEFAULT_SERVER_STATE
        )

        current = servers_state.get(
            name
        )

        if isinstance(
            current,
            dict,
        ):
            merged.update(
                current
            )

        servers_state[name] = merged

    # 删除已经不再监控的旧服务器状态
    for old in list(
        servers_state
    ):

        if old not in server_names:

            del servers_state[
                old
            ]

    return state


# ============================================================
# Shell / SSH
# ============================================================

def run_command(
    argv,
    stdin_text=None,
    timeout=25,
):

    try:

        proc = subprocess.run(
            argv,
            input=stdin_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

        return (
            proc.returncode,
            proc.stdout,
            proc.stderr,
        )

    except subprocess.TimeoutExpired as e:

        stdout = (
            e.stdout
            if isinstance(
                e.stdout,
                str,
            )
            else ""
        )

        stderr = (
            e.stderr
            if isinstance(
                e.stderr,
                str,
            )
            else ""
        )

        return (
            124,
            stdout,
            (
                stderr
                + "\ncommand timeout"
            ).strip(),
        )

    except Exception as e:

        return (
            125,
            "",
            str(e),
        )


# ============================================================
# 异常时诊断脚本
# ============================================================

COLLECTOR_SCRIPT = r"""set -u

echo "===== SYSTEM ====="

date '+time=%F %T %z'

uptime 2>/dev/null || true

free -m 2>/dev/null \
    | head -n 5 \
    || true


echo "-- vmstat --"

vmstat 1 2 2>/dev/null \
    | tail -n 3 \
    || true


echo "-- df --"

df -h \
    -x tmpfs \
    -x devtmpfs \
    2>/dev/null \
    | head -n 20 \
    || true


echo "-- sockets --"

ss -s 2>/dev/null \
    || true


echo "===== SERVICES ====="

for p in nginx php-fpm mysqld mariadbd
do

    if pgrep -x "$p" >/dev/null 2>&1
    then
        echo "$p=running"
    else
        echo "$p=not_running"
    fi

done


echo "===== TOP_PROCESSES ====="

ps \
    -eo pid,ppid,user,%cpu,%mem,etime,cmd \
    --sort=-%cpu \
    2>/dev/null \
    | head -n 21 \
    || true


echo "===== NGINX_RECENT ====="

if [ -d /www/wwwlogs ]
then

    find \
        /www/wwwlogs \
        -maxdepth 1 \
        -type f \
        -name '*.log' \
        -mmin -15 \
        -printf '%T@ %p\n' \
        2>/dev/null \
    | sort -nr \
    | head -n 6 \
    | cut -d' ' -f2- \
    | while IFS= read -r f
    do

        [ -r "$f" ] || continue

        echo "--- FILE:$f ---"

        tail -n 250 "$f" 2>/dev/null \
        | awk '

            NF >= 9 {

                ip[$1]++

                uri[$7]++

                code[$9]++

                total++

            }

            END {

                print "total_samples=" total

                for (k in ip)
                    print "IP", ip[k], k

                for (k in uri)
                    print "URI", uri[k], k

                for (k in code)
                    print "STATUS", code[k], k

            }

        ' \
        | sort -k2,2nr \
        | head -n 80

    done

else

    echo "/www/wwwlogs not found"

fi


echo "===== PHP_SLOW ====="

found_php=0

for f in \
    /www/server/php/*/var/log/slow.log \
    /www/server/php/*/var/log/php-fpm.log \
    /www/server/php/*/var/log/error.log

do

    [ -f "$f" ] || continue

    found_php=1

    echo "--- FILE:$f ---"

    tail -n 80 "$f" \
        2>/dev/null \
        || true

done


[ "$found_php" -eq 1 ] \
    || echo "no php slow/error log found in default paths"


echo "===== MYSQL ====="

MYSQL_BIN=""

for b in \
    /www/server/mysql/bin/mysql \
    mysql \
    mariadb

do

    if command -v "$b" >/dev/null 2>&1
    then

        MYSQL_BIN="$b"

        break

    fi


    if [ -x "$b" ]
    then

        MYSQL_BIN="$b"

        break

    fi

done


if [ -n "$MYSQL_BIN" ]
then

    timeout 4 \
        "$MYSQL_BIN" \
        --connect-timeout=3 \
        -N \
        -e "

        SHOW GLOBAL STATUS
        WHERE Variable_name IN (
            'Threads_running',
            'Slow_queries',
            'Questions'
        );

        SHOW FULL PROCESSLIST;

        " \
        2>&1 \
        | head -n 120 \
        || true

else

    echo "mysql client not found"

fi


echo "===== MYSQL_SLOW ====="

found_mysql=0

for f in \
    /www/server/data/*-slow.log \
    /www/server/data/mysql-slow.log \
    /var/log/mysql/mysql-slow.log \
    /var/log/mysql/mariadb-slow.log

do

    [ -f "$f" ] || continue

    found_mysql=1

    echo "--- FILE:$f ---"

    tail -n 80 "$f" \
        2>/dev/null \
        || true

done


[ "$found_mysql" -eq 1 ] \
    || echo "no mysql slow log found in default paths"
"""


def collect_diagnostics(
    server,
    max_chars,
):

    # Server A 本机
    if server.get(
        "local",
        False,
    ):

        rc, out, err = run_command(

            [
                "bash",
                "-s",
            ],

            COLLECTOR_SCRIPT,

            30,
        )

    # Server B/C/D/E
    else:

        host = str(
            server.get(
                "ssh_host"
            )
            or ""
        )

        user = str(
            server.get(
                "ssh_user"
            )
            or "root"
        )

        port = str(
            server.get(
                "ssh_port"
            )
            or 22
        )

        argv = [

            "ssh",

            "-T",

            "-o",
            "BatchMode=yes",

            "-o",
            "ConnectTimeout=5",

            "-o",
            "ConnectionAttempts=1",

            "-o",
            "ServerAliveInterval=5",

            "-o",
            "ServerAliveCountMax=1",

            "-p",
            port,
        ]

        if server.get(
            "ssh_key"
        ):

            argv += [
                "-i",
                str(
                    server["ssh_key"]
                ),
            ]

        argv += [

            f"{user}@{host}",

            "bash",

            "-s",
        ]

        rc, out, err = run_command(

            argv,

            COLLECTOR_SCRIPT,

            35,
        )

    if (
        rc != 0
        and err
    ):

        out += (
            f"\n"
            f"===== COLLECTOR_ERROR rc={rc} =====\n"
            f"{err}\n"
        )

    return clamp_text(
        out,
        max_chars,
    )


# ============================================================
# 服务轻量检查
# ============================================================

SERVICE_CHECK_SCRIPT = r"""set -u

nginx=0
php=0
mysql=0


pgrep -x nginx \
    >/dev/null 2>&1 \
    && nginx=1 \
    || true


if \
    pgrep -x php-fpm >/dev/null 2>&1 \
    || \
    pgrep -f 'php-fpm: master' >/dev/null 2>&1

then

    php=1

fi


if \
    pgrep -x mysqld >/dev/null 2>&1 \
    || \
    pgrep -x mariadbd >/dev/null 2>&1

then

    mysql=1

fi


echo "nginx=$nginx"
echo "php=$php"
echo "mysql=$mysql"
"""


def check_services(
    server,
):

    if server.get(
        "local",
        False,
    ):

        rc, out, err = run_command(

            [
                "bash",
                "-s",
            ],

            SERVICE_CHECK_SCRIPT,

            8,
        )

    else:

        host = str(
            server.get(
                "ssh_host"
            )
            or ""
        )

        user = str(
            server.get(
                "ssh_user"
            )
            or "root"
        )

        port = str(
            server.get(
                "ssh_port"
            )
            or 22
        )

        argv = [

            "ssh",

            "-T",

            "-o",
            "BatchMode=yes",

            "-o",
            "ConnectTimeout=4",

            "-o",
            "ConnectionAttempts=1",

            "-o",
            "ServerAliveInterval=4",

            "-o",
            "ServerAliveCountMax=1",

            "-p",
            port,
        ]

        if server.get(
            "ssh_key"
        ):

            argv += [

                "-i",

                str(
                    server["ssh_key"]
                ),
            ]

        argv += [

            f"{user}@{host}",

            "bash",

            "-s",
        ]

        rc, out, err = run_command(

            argv,

            SERVICE_CHECK_SCRIPT,

            10,
        )

    if rc != 0:

        LOGGER.warning(

            "[%s] 服务状态检查失败 rc=%s err=%s",

            server["name"],

            rc,

            (err or "")[:160],
        )

        # SSH 出错不直接判断服务宕机
        return None

    result = {}

    for line in out.splitlines():

        if "=" not in line:
            continue

        k, v = line.strip().split(
            "=",
            1,
        )

        if k in (
            "nginx",
            "php",
            "mysql",
        ):

            result[k] = (
                v == "1"
            )

    if not all(
        k in result
        for k in (
            "nginx",
            "php",
            "mysql",
        )
    ):

        return None

    return result


# ============================================================
# DeepSeek
# ============================================================

AI_SYSTEM_PROMPT = """你是资深 Linux/Nginx/PHP-FPM/MySQL 运维工程师。

严格规则：

1. 只根据提供的数据判断，禁止虚构。
2. 优先定位具体进程、域名/日志文件、IP、URI、PHP文件、SQL。
3. 最多指出3个可能原因。
4. 明确给出置信度：高/中/低。
5. 最多给3条立即可执行建议。
6. 不默认建议重启服务器。
7. 证据不足必须明确写“证据不足”。
8. 不复述大段原始日志。
9. 不使用Markdown表格。
10. 总输出控制在450个中文字符左右。

固定格式：

【判断】
主要原因：
置信度：

【证据】
1.
2.
3.

【建议】
1.
2.
3.
"""


def deepseek_analyze(
    config,
    server_name,
    metrics,
    duration_min,
    diagnostics,
):

    ai = config.get(
        "deepseek",
        {},
    )

    api_key = str(
        ai.get(
            "api_key"
        )
        or ""
    ).strip()

    if not api_key:

        return (
            "AI未配置："
            "已完成日志采集，"
            "但未进行DeepSeek分析。"
        )

    payload = {

        "model": str(
            ai.get(
                "model"
            )
            or "deepseek-flash"
        ),

        "messages": [

            {
                "role": "system",
                "content": AI_SYSTEM_PROMPT,
            },

            {
                "role": "user",

                "content": (

                    f"服务器：{server_name}\n"

                    f"时间：{now_text()}\n"

                    f"异常持续约："
                    f"{duration_min}分钟\n"

                    f"当前指标："
                    f"{json.dumps(metrics, ensure_ascii=False)}\n"

                    f"----- BEGIN DIAGNOSTICS -----\n"

                    f"{diagnostics}\n"

                    f"----- END DIAGNOSTICS -----"
                ),
            },
        ],

        "temperature": 0.1,

        "max_tokens": int(
            ai.get(
                "max_tokens",
                650,
            )
        ),

        "stream": False,
    }

    try:

        result = http_post_json(

            str(
                ai.get(
                    "url"
                )
                or
                "https://api.deepseek.com/chat/completions"
            ),

            payload,

            {
                "Authorization":
                    f"Bearer {api_key}"
            },

            float(
                ai.get(
                    "timeout",
                    25,
                )
            ),
        )

        content = (
            result
            .get(
                "choices",
                [{}],
            )[0]
            .get(
                "message",
                {},
            )
            .get(
                "content",
                "",
            )
        )

        if (
            not isinstance(
                content,
                str,
            )
            or
            not content.strip()
        ):

            raise RuntimeError(
                "DeepSeek返回内容为空"
            )

        return clamp_text(

            content.strip(),

            int(
                ai.get(
                    "max_output_chars",
                    1200,
                )
            ),
        )

    except Exception as e:

        LOGGER.error(

            "[%s] DeepSeek调用失败: %s",

            server_name,

            e,
        )

        return (
            f"AI分析失败："
            f"{type(e).__name__}: "
            f"{str(e)[:160]}"
        )


# ============================================================
# Telegram
# ============================================================

def telegram_send(
    config,
    text,
):

    tg = config.get(
        "telegram",
        {},
    )

    token = str(
        tg.get(
            "bot_token"
        )
        or ""
    ).strip()

    chat_id = str(
        tg.get(
            "chat_id"
        )
        or ""
    ).strip()

    if (
        not token
        or
        not chat_id
    ):

        LOGGER.warning(
            "Telegram未配置，跳过推送"
        )

        return False

    try:

        result = http_post_json(

            f"https://api.telegram.org/"
            f"bot{token}/sendMessage",

            {
                "chat_id":
                    chat_id,

                "text":
                    clamp_text(
                        text,
                        3900,
                    ),

                "disable_web_page_preview":
                    True,
            },

            {},

            10,
        )

        return bool(
            isinstance(
                result,
                dict,
            )
            and
            result.get("ok")
        )

    except Exception as e:

        LOGGER.error(
            "Telegram发送失败: %s",
            e,
        )

        return False


def mention(
    config,
):

    username = str(

        config.get(
            "telegram",
            {},
        ).get(
            "mention"
        )

        or ""

    ).strip()

    if not username:
        return ""

    if username.startswith("@"):
        return username

    return (
        "@"
        + username
    )


# ============================================================
# TG 信息
# ============================================================

def incident_message(
    config,
    name,
    m,
    duration,
    ai,
):

    return (

        f"🚨 {name} 高负载告警\n"

        f"时间：{now_text()}\n"

        f"CPU：{m['cpu']:.1f}%  "
        f"内存：{m['mem_pct']:.1f}%\n"

        f"Load："
        f"{m['load1']:.2f} / "
        f"{m['load5']:.2f} / "
        f"{m['load15']:.2f}\n"

        f"持续：约 {duration} 分钟\n\n"

        f"{ai}\n\n"

        f"{mention(config)}"

    ).strip()


def critical_message(
    config,
    name,
    m,
    duration,
):

    return (

        f"🔥 {name} 负载持续恶化\n"

        f"CPU：{m['cpu']:.1f}%\n"

        f"Load："
        f"{m['load1']:.2f} / "
        f"{m['load5']:.2f} / "
        f"{m['load15']:.2f}\n"

        f"已持续：约 {duration} 分钟\n"

        f"本事件已发过AI诊断，"
        f"不重复调用AI。\n"

        f"{mention(config)}"

    ).strip()


def recovery_message(
    name,
    m,
    duration,
):

    return (

        f"✅ {name} 已恢复\n"

        f"CPU：{m['cpu']:.1f}%  "

        f"内存：{m['mem_pct']:.1f}%\n"

        f"Load："
        f"{m['load1']:.2f} / "
        f"{m['load5']:.2f} / "
        f"{m['load15']:.2f}\n"

        f"本次异常持续："
        f"约 {duration} 分钟"
    )


def api_failure_message(
    config,
    name,
    count,
    err,
):

    return (

        f"⚠️ {name} 监控连接异常\n"

        f"宝塔API连续失败："
        f"{count}次\n"

        f"错误："
        f"{type(err).__name__}: "
        f"{str(err)[:180]}\n"

        f"这不代表服务器宕机，"
        f"优先检查面板/API白名单/网络。\n"

        f"{mention(config)}"

    ).strip()


def service_message(
    config,
    name,
    status,
    recovered=False,
):

    if recovered:

        return (

            f"✅ {name} 服务状态已恢复\n"

            f"Nginx："
            f"{'正常' if status.get('nginx') else '异常'}  "

            f"PHP："
            f"{'正常' if status.get('php') else '异常'}  "

            f"MySQL："
            f"{'正常' if status.get('mysql') else '异常'}"
        )

    bad = [

        k.upper()

        for k, ok
        in status.items()

        if not ok
    ]

    return (

        f"🚨 {name} 服务异常\n"

        f"异常服务："
        f"{', '.join(bad) if bad else '未知'}\n"

        f"Nginx："
        f"{'正常' if status.get('nginx') else '异常'}  "

        f"PHP："
        f"{'正常' if status.get('php') else '异常'}  "

        f"MySQL："
        f"{'正常' if status.get('mysql') else '异常'}\n"

        f"{mention(config)}"

    ).strip()


# ============================================================
# 服务状态检测
# ============================================================

def maybe_check_services(
    config,
    server,
    st,
):

    monitor = config[
        "monitor"
    ]

    interval = int(
        monitor.get(
            "service_check_interval_sec",
            300,
        )
    )

    now = int(
        time.time()
    )

    if (
        now
        - int(
            st.get(
                "last_service_check",
                0,
            )
        )
        < interval
    ):

        return

    st[
        "last_service_check"
    ] = now

    status = check_services(
        server
    )

    if status is None:
        return

    st[
        "service_status"
    ] = status

    required = server.get(

        "required_services",

        [
            "nginx",
            "php",
            "mysql",
        ],
    )

    down = [

        name

        for name
        in required

        if not status.get(
            name,
            False,
        )
    ]

    if down:

        st[
            "service_fail_count"
        ] = (
            int(
                st.get(
                    "service_fail_count",
                    0,
                )
            )
            + 1
        )

        if (

            st[
                "service_fail_count"
            ]
            >= int(
                monitor.get(
                    "service_fail_trigger",
                    2,
                )
            )

            and

            not st.get(
                "service_alert_sent"
            )

        ):

            if telegram_send(

                config,

                service_message(
                    config,
                    server["name"],
                    status,
                ),
            ):

                st[
                    "service_alert_sent"
                ] = True

    else:

        was_alerted = bool(
            st.get(
                "service_alert_sent"
            )
        )

        st[
            "service_fail_count"
        ] = 0

        st[
            "service_alert_sent"
        ] = False

        if was_alerted:

            telegram_send(

                config,

                service_message(
                    config,
                    server["name"],
                    status,
                    recovered=True,
                ),
            )


# ============================================================
# 配置检查
# ============================================================

def validate_config(
    config,
):

    servers = config.get(
        "servers"
    )

    if (
        not isinstance(
            servers,
            list,
        )
        or
        not servers
    ):

        raise ValueError(
            "config.json 必须包含非空 servers 数组"
        )

    names = set()

    for i, server in enumerate(
        servers
    ):

        if not isinstance(
            server,
            dict,
        ):

            raise ValueError(
                f"servers[{i}] 必须是对象"
            )

        for key in (
            "name",
            "bt_url",
            "bt_api_key",
        ):

            if not str(
                server.get(key)
                or ""
            ).strip():

                raise ValueError(
                    f"servers[{i}] 缺少 {key}"
                )

        if server["name"] in names:

            raise ValueError(
                "服务器名称重复: "
                + server["name"]
            )

        names.add(
            server["name"]
        )

        if (
            not server.get(
                "local",
                False,
            )
            and
            not str(
                server.get(
                    "ssh_host"
                )
                or ""
            ).strip()
        ):

            raise ValueError(
                f"{server['name']} "
                f"非本机但未配置 ssh_host"
            )

    monitor = config.setdefault(
        "monitor",
        {},
    )

    defaults = {

        "interval_sec":
            60,

        "cpu_threshold":
            80,

        "trigger_count":
            5,

        "recover_cpu":
            70,

        "recover_count":
            5,

        "critical_cpu":
            95,

        "critical_after_min":
            10,

        "api_fail_alert_count":
            5,

        "diagnostic_max_chars":
            24000,

        "service_check_interval_sec":
            300,

        "service_fail_trigger":
            2,
    }

    for key, value in defaults.items():

        monitor.setdefault(
            key,
            value,
        )

    if int(
        monitor["interval_sec"]
    ) < 30:

        raise ValueError(
            "interval_sec 必须 >= 30"
        )

    if (
        int(
            monitor["trigger_count"]
        )
        < 1
        or
        int(
            monitor["recover_count"]
        )
        < 1
    ):

        raise ValueError(
            "trigger_count/recover_count 必须 >= 1"
        )


# ============================================================
# 单台服务器状态机
# ============================================================

def process_server(
    config,
    server,
    state,
):

    monitor = config[
        "monitor"
    ]

    name = str(
        server["name"]
    )

    st = state[
        "servers"
    ][
        name
    ]

    # --------------------------------------------------------
    # 宝塔 API 状态获取
    # --------------------------------------------------------

    try:

        metrics = parse_bt_metrics(

            bt_api_call(
                server,
                "GetNetWork",
            )
        )

        st[
            "last_poll_ok"
        ] = int(
            time.time()
        )

        st[
            "api_fail_count"
        ] = 0

        st[
            "api_alert_sent"
        ] = False

        st[
            "last_metrics"
        ] = metrics

        # 每5分钟做一次服务进程检查
        maybe_check_services(
            config,
            server,
            st,
        )

    except Exception as e:

        st[
            "api_fail_count"
        ] = (

            int(
                st.get(
                    "api_fail_count",
                    0,
                )
            )

            + 1
        )

        LOGGER.warning(

            "[%s] API失败(%s): %s",

            name,

            st[
                "api_fail_count"
            ],

            e,
        )

        threshold = int(
            monitor[
                "api_fail_alert_count"
            ]
        )

        if (

            st[
                "api_fail_count"
            ]
            >= threshold

            and

            not st.get(
                "api_alert_sent"
            )

        ):

            if telegram_send(

                config,

                api_failure_message(
                    config,
                    name,
                    st[
                        "api_fail_count"
                    ],
                    e,
                ),
            ):

                st[
                    "api_alert_sent"
                ] = True

        return

    # --------------------------------------------------------
    # CPU 状态机
    # --------------------------------------------------------

    cpu = float(
        metrics["cpu"]
    )

    interval_sec = int(
        monitor[
            "interval_sec"
        ]
    )

    trigger_count = int(
        monitor[
            "trigger_count"
        ]
    )

    recover_count = int(
        monitor[
            "recover_count"
        ]
    )

    # ========================================================
    # NORMAL 状态
    # ========================================================

    if not st.get(
        "incident_active"
    ):

        st[
            "recover_count"
        ] = 0

        if cpu > float(
            monitor[
                "cpu_threshold"
            ]
        ):

            st[
                "high_count"
            ] = (

                int(
                    st.get(
                        "high_count",
                        0,
                    )
                )

                + 1
            )

        else:

            st[
                "high_count"
            ] = 0

        # ----------------------------------------------------
        # CPU >80 连续达到要求
        # ----------------------------------------------------

        if (
            st[
                "high_count"
            ]
            >= trigger_count
        ):

            st[
                "incident_active"
            ] = True

            st[
                "critical_sent"
            ] = False

            st[
                "incident_started_at"
            ] = (

                int(
                    time.time()
                )

                -

                (
                    max(
                        0,
                        trigger_count - 1,
                    )

                    * interval_sec
                )
            )

            duration = max(

                1,

                int(
                    (
                        time.time()
                        -
                        st[
                            "incident_started_at"
                        ]
                    )
                    / 60
                ),
            )

            LOGGER.warning(

                "[%s] INCIDENT cpu=%.1f high_count=%s",

                name,

                cpu,

                st[
                    "high_count"
                ],
            )

            # ================================================
            # 仅第一次抓日志
            # ================================================

            diagnostics = collect_diagnostics(

                server,

                int(
                    monitor[
                        "diagnostic_max_chars"
                    ]
                ),
            )

            # ================================================
            # 仅第一次调用AI
            # ================================================

            ai_text = deepseek_analyze(

                config,

                name,

                metrics,

                duration,

                diagnostics,
            )

            msg = incident_message(

                config,

                name,

                metrics,

                duration,

                ai_text,
            )

            # ================================================
            # TG失败只缓存消息
            # 不重新抓日志、不重新AI
            # ================================================

            if telegram_send(
                config,
                msg,
            ):

                st[
                    "pending_incident_message"
                ] = ""

            else:

                st[
                    "pending_incident_message"
                ] = msg

        return

    # ========================================================
    # INCIDENT 状态
    # ========================================================

    # TG主告警发送失败
    # 下轮只重试消息
    pending = str(
        st.get(
            "pending_incident_message"
        )
        or ""
    )

    if pending:

        if telegram_send(
            config,
            pending,
        ):

            st[
                "pending_incident_message"
            ] = ""

    incident_started = int(
        st.get(
            "incident_started_at"
        )
        or time.time()
    )

    duration = max(

        1,

        int(
            (
                time.time()
                - incident_started
            )
            / 60
        ),
    )

    # --------------------------------------------------------
    # 恢复判断
    # --------------------------------------------------------

    if cpu < float(
        monitor[
            "recover_cpu"
        ]
    ):

        st[
            "recover_count"
        ] = (

            int(
                st.get(
                    "recover_count",
                    0,
                )
            )

            + 1
        )

    else:

        st[
            "recover_count"
        ] = 0

    # --------------------------------------------------------
    # RECOVERY
    # --------------------------------------------------------

    if (
        st[
            "recover_count"
        ]
        >= recover_count
    ):

        LOGGER.info(

            "[%s] RECOVERY cpu=%.1f",

            name,

            cpu,
        )

        # TG成功以后才真正关闭事故
        if telegram_send(

            config,

            recovery_message(
                name,
                metrics,
                duration,
            ),
        ):

            fresh = deepcopy(
                DEFAULT_SERVER_STATE
            )

            fresh[
                "last_poll_ok"
            ] = st.get(
                "last_poll_ok",
                int(time.time()),
            )

            fresh[
                "last_metrics"
            ] = metrics

            fresh[
                "last_service_check"
            ] = st.get(
                "last_service_check",
                0,
            )

            fresh[
                "service_status"
            ] = st.get(
                "service_status",
                {},
            )

            state[
                "servers"
            ][
                name
            ] = fresh

        return

    # --------------------------------------------------------
    # CRITICAL
    # --------------------------------------------------------

    critical_due = (

        cpu > float(
            monitor[
                "critical_cpu"
            ]
        )

        and

        duration >= int(
            monitor[
                "critical_after_min"
            ]
        )
    )

    if (
        critical_due
        and
        not st.get(
            "critical_sent"
        )
    ):

        if telegram_send(

            config,

            critical_message(
                config,
                name,
                metrics,
                duration,
            ),
        ):

            st[
                "critical_sent"
            ] = True


# ============================================================
# 一轮扫描
# ============================================================

def one_cycle(
    config,
    state,
):

    for server in config[
        "servers"
    ]:

        if STOP:
            break

        try:

            process_server(
                config,
                server,
                state,
            )

        except Exception:

            LOGGER.exception(

                "[%s] 单机处理出现未捕获异常",

                server["name"],
            )

        finally:

            try:

                atomic_write_json(
                    STATE_FILE,
                    state,
                )

            except Exception:

                LOGGER.exception(
                    "保存状态文件失败"
                )


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(

        description=
            "轻量服务器高负载监控与AI诊断"
    )

    parser.add_argument(

        "-c",

        "--config",

        default=str(
            DEFAULT_CONFIG
        ),

        help=
            "config.json 路径",
    )

    parser.add_argument(

        "--once",

        action="store_true",

        help=
            "只执行一轮后退出，用于测试",
    )

    parser.add_argument(

        "--test-config",

        action="store_true",

        help=
            "只检查配置后退出",
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # 加载配置
    # --------------------------------------------------------

    try:

        config = load_json(
            Path(
                args.config
            )
        )

        validate_config(
            config
        )

    except Exception as e:

        print(

            f"配置错误: {e}",

            file=sys.stderr,
        )

        return 2

    # --------------------------------------------------------
    # 只检查配置
    # --------------------------------------------------------

    if args.test_config:

        print(
            "config OK"
        )

        return 0

    server_names = [

        str(
            s["name"]
        )

        for s
        in config["servers"]
    ]

    state = load_state(
        server_names
    )

    interval = int(
        config[
            "monitor"
        ][
            "interval_sec"
        ]
    )

    LOGGER.info(

        "Server Guard started, "
        "servers=%s interval=%ss",

        ",".join(
            server_names
        ),

        interval,
    )

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------

    while not STOP:

        started = time.monotonic()

        one_cycle(
            config,
            state,
        )

        if args.once:
            break

        elapsed = (
            time.monotonic()
            - started
        )

        sleep_sec = max(
            1.0,
            interval - elapsed,
        )

        end_at = (
            time.monotonic()
            + sleep_sec
        )

        while not STOP:

            remaining = (
                end_at
                - time.monotonic()
            )

            if remaining <= 0:
                break

            time.sleep(
                min(
                    1.0,
                    remaining,
                )
            )

    LOGGER.info(
        "Server Guard stopped"
    )

    return 0


if __name__ == "__main__":

    raise SystemExit(
        main()
    )