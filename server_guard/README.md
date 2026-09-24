# Server Guard

轻量级 Linux 服务器负载监控、日志采集、AI 诊断与 Telegram 告警工具。

适用于 Debian + 宝塔面板 + Nginx + PHP-FPM + MySQL 环境。

## 功能

Server Guard 使用一台服务器作为统一监控节点，对多台服务器进行状态监测。

主要功能：

- 每 60 秒检查服务器 CPU、内存和 Load
- 每 5 分钟检查 Nginx、PHP-FPM、MySQL 服务状态
- CPU 持续高负载时自动进入异常状态
- 自动提取 Nginx、PHP、MySQL 和系统关键日志
- 自动调用 DeepSeek API 分析异常原因
- 自动通过 Telegram Bot 推送诊断结果
- 防止重复告警和 TG 刷屏
- CPU 恢复后自动发送恢复通知
- 支持多服务器统一监控

---

## 工作流程

```text
Server A
   │
   ├── 每60秒检测 CPU / RAM / Load
   │
   ├── Server B
   ├── Server C
   ├── Server D
   └── Server E

CPU > 80%
   │
连续5次
   │
   ▼
进入 INCIDENT
   │
   ├── 系统状态
   ├── TOP进程
   ├── Nginx日志
   ├── PHP Slow Log
   ├── MySQL Processlist
   └── MySQL Slow Log
   │
   ▼
本地整理
   │
   ▼
DeepSeek API
   │
   ▼
AI诊断
   │
   ▼
Telegram Bot
```

---

## 告警机制

默认高负载阈值：

```text
CPU > 80%
连续 5 分钟
```

触发一次：

```text
🚨 INCIDENT
```

如果：

```text
CPU > 95%
持续超过 10 分钟
```

额外发送一次：

```text
🔥 CRITICAL
```

事故期间不会每分钟重复调用 AI，也不会重复发送 Telegram。

服务器恢复条件：

```text
CPU < 70%
连续 5 分钟
```

发送：

```text
✅ RECOVERY
```

因此一次完整的服务器异常通常最多产生：

```text
1 × INCIDENT
1 × CRITICAL
1 × RECOVERY
```

避免 Telegram 告警刷屏。

---

## 项目结构

```text
/opt/server_guard/
│
├── server_guard.py
├── config.json
├── config.example.json
├── README.md
├── .gitignore
│
├── logs/
│   └── server_guard.log
│
└── state/
    └── state.json
```

其中：

```text
server_guard.py
```

主监控程序。

```text
config.json
```

真实运行配置，包含 API Key、Telegram Token、服务器地址等敏感数据。

**禁止提交至 GitHub。**

```text
config.example.json
```

GitHub 配置模板。

```text
state/state.json
```

保存服务器当前告警状态，防止程序重启后重复发送告警。

```text
logs/server_guard.log
```

Server Guard 自身运行日志。

---

## 环境要求

推荐：

```text
Debian 12
Python 3.9+
宝塔面板
Nginx
PHP-FPM
MySQL / MariaDB
```

程序主要使用 Python 标准库，不依赖大型 Python 第三方模块。

检查 Python：

```bash
python3 --version
```

---

## 安装

创建目录：

```bash
mkdir -p /opt/server_guard/{logs,state}
cd /opt/server_guard
```

克隆项目：

```bash
git clone https://github.com/YOUR_USERNAME/server-guard.git /opt/server_guard
```

复制配置文件：

```bash
cp config.example.json config.json
```

修改：

```bash
nano config.json
```

填写：

- 宝塔 API Key
- DeepSeek API Key
- Telegram Bot Token
- Telegram Chat ID
- Telegram 用户名
- Server A/B/C/D/E 地址
- SSH 地址和密钥

---

## 宝塔 API

需要在被监控服务器开启宝塔 API。

只建议允许监控服务器 Server A 的 IP 访问宝塔 API。

不要把宝塔 API 对所有公网 IP 开放。

配置示例：

```json
{
  "name": "Server-B",
  "local": false,

  "bt_url": "https://SERVER_B_IP:8888",
  "bt_api_key": "YOUR_BT_API_KEY",

  "verify_tls": false,

  "ssh_host": "SERVER_B_IP",
  "ssh_port": 22,
  "ssh_user": "root",
  "ssh_key": "/root/.ssh/id_ed25519"
}
```

---

## SSH

Server A 需要能够免密 SSH 登录 Server B/C/D/E。

推荐使用 SSH Key：

```bash
ssh-keygen -t ed25519
```

复制公钥：

```bash
ssh-copy-id root@SERVER_B_IP
```

测试：

```bash
ssh root@SERVER_B_IP
```

能够直接登录且不需要输入密码即可。

依次配置：

```text
Server B
Server C
Server D
Server E
```

---

## DeepSeek

配置：

```json
"deepseek": {
  "api_key": "YOUR_DEEPSEEK_API_KEY",
  "url": "https://api.deepseek.com/chat/completions",
  "model": "deepseek-flash",
  "timeout": 25,
  "max_tokens": 650,
  "max_output_chars": 1200
}
```

AI 仅在服务器正式进入异常状态时调用。

正常监控期间不会调用 DeepSeek API。

---

## Telegram

配置：

```json
"telegram": {
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "chat_id": "YOUR_CHAT_ID",
  "mention": "@YOUR_USERNAME"
}
```

发生异常后会发送类似：

```text
🚨 Server-B 高负载告警

CPU：94%
内存：72%

Load：
15.3 / 13.8 / 9.2

持续：
5分钟

【判断】
主要原因：
PHP-FPM异常占用CPU

【证据】
1. 多个php-fpm进程CPU超过40%
2. 某站点index.php请求量异常
3. MySQL Threads_running正常

【建议】
1. 检查异常站点PHP文件
2. 检查高频访问IP
3. 根据日志决定是否进行限流

@USERNAME
```

---

## 配置检查

修改完 `config.json` 后：

```bash
cd /opt/server_guard

python3 server_guard.py \
  --test-config
```

正常：

```text
config OK
```

---

## 手动测试

只执行一次监控：

```bash
python3 server_guard.py --once
```

查看日志：

```bash
tail -100 /opt/server_guard/logs/server_guard.log
```

实时查看：

```bash
tail -f /opt/server_guard/logs/server_guard.log
```

---

## 正式运行

建议使用 systemd 或宝塔进程守护运行。

启动：

```bash
python3 /opt/server_guard/server_guard.py
```

程序默认：

```text
每60秒执行一次巡检
```

---

## 默认阈值

```json
{
  "interval_sec": 60,

  "cpu_threshold": 80,
  "trigger_count": 5,

  "recover_cpu": 70,
  "recover_count": 5,

  "critical_cpu": 95,
  "critical_after_min": 10,

  "service_check_interval_sec": 300,
  "service_fail_trigger": 2
}
```

含义：

```text
CPU >80% × 5分钟
→ INCIDENT

CPU >95% 且持续10分钟
→ CRITICAL

CPU <70% × 5分钟
→ RECOVERY
```

---

## 安全说明

禁止把真实的以下信息上传 GitHub：

```text
DeepSeek API Key
Telegram Bot Token
宝塔 API Key
SSH Private Key
MySQL密码
真实 config.json
```

`.gitignore` 至少需要包含：

```gitignore
config.json

logs/
state/

__pycache__/
*.pyc

*.log
*.tmp

*.pem
*.key

id_rsa
id_ed25519
```

推荐 GitHub 仓库设置为：

```text
Private
```

即使使用 Private Repository，也不要提交 API Key 和 Token。

---

## 更新代码

以后 GitHub 修改代码后，服务器执行：

```bash
cd /opt/server_guard

git pull
```

然后重启 Server Guard 服务即可。

本地修改后上传：

```bash
cd /opt/server_guard

git status

git add server_guard.py
git add config.example.json
git add README.md

git commit -m "Update server guard"

git push
```

---

## 注意

Server Guard 的设计目标不是实时保存大量日志，而是在服务器真正出现持续异常时：

```text
发现异常
→ 截取关键证据
→ AI分析
→ TG通知
```

正常状态下保持尽可能低的资源消耗。

特别适合已经运行大量 Nginx + PHP-FPM + MySQL 网站的服务器环境。
