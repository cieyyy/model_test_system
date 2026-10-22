# 模型测试平台

整合本机模型测试脚本、流式并发曲线、测试记录、在线编辑报告和不同中转站的模型对比。侧栏只有新建测试、数据展示、测试记录、模型对比四个入口。测试支持单个项目或所有项目，真实请求需要先预览计划并确认。

## Docker 部署

所有常驻服务统一使用 Docker Compose：
- app：Python 3.12 + FastAPI / Uvicorn，包含前端和全部测试脚本，单 worker、非 root 运行。
- proxy：Nginx，负责 HTTPS、登录认证、上传和长连接代理，仅对公网开放 80/443。
- certbot：检查并续期 Let's Encrypt IPv4 短期证书，每 6 小时检查，失败后 30 分钟重试。Nginx 检测证书变化后自动重载。

不需要宿主机 Python 虚拟环境、宿主机 Nginx、独立数据库或 Redis。SQLite 和全部上传／生成文件保存在服务器，不打包进镜像，也不提交 GitHub。宿主机仅保留 Docker 和安装／更新所用的 shell、Git、curl、Python 标准库备份工具。

适用于 Ubuntu 24.04 的独占服务器，开放 22/80/443 端口：

```bash
git clone https://github.com/cieyyy/model_test_system.git /opt/model-test/repository
bash /opt/model-test/repository/deploy/install.sh YOUR_SERVER_IP
```

安装程序配置 Docker、持久目录、认证和 IP 证书，并构建、测试和启动三个容器。初始登录信息仅保存在服务器 `/root/model-test-login.txt`。已有其他站点的服务器需要先合并入口配置，不能直接使用独占端口安装。

旧 systemd 部署也执行同一安装命令：构建和离线测试期间旧站点持续运行；切换前检查没有活动任务、阻止新写入、备份数据，然后停止旧服务并启用容器。保留原数据路径、secret.key、登录信息和 HTTPS 证书，不导出再导入数据库。启动或 HTTPS 检查失败时自动恢复旧服务与本次切换前的数据。

## 持久化与磁盘控制

- `/var/lib/model-test/data`：SQLite、密钥、导入对象、测试证据、报告及版本。
- `/var/lib/model-test/media`：图片、视频素材。
- `/var/lib/model-test/results`、`tiktoken`、`acceptance-fixtures`：脚本结果、分词缓存、独立验收素材。
- `/etc/model-test/platform.env`：应用配置，参考 [deploy/platform.env.example](deploy/platform.env.example)。
- `/etc/model-test/docker.env`：当前镜像版本和与宿主机一致的运行 UID/GID，由更新脚本维护。
- `/etc/model-test/htpasswd`：认证哈希文件；保持 root:101、640 权限，供 Nginx 容器读取。
- `/etc/letsencrypt`、`/var/www/model-test-acme`：证书、账户及续期验证目录。
- `/opt/model-test/releases/<commit>`、`current`：版本化 Compose 配置和当前版本链接。
- `/var/backups/model-test`：更新前的数据库、密钥和证据快照，仅 root 可读。

原路径直接挂载进容器，历史证据中的绝对路径仍然有效。全部业务数据位于镜像之外；更新不会覆盖，`model-test-ctl down` 也不会删除。不得删除 secret.key，否则无法解密已保存的 API Key。

只保留当前与前一版应用镜像／代码，以及最近 5 份更新快照。不可变证据和导入对象使用硬链接备份，避免多份复制；容器日志每个服务最多 2 × 5MB，临时目录在内存中并设定上限。不会执行影响其他项目的 `docker system prune`。Docker 基础镜像和构建缓存可由 `docker system df` 查看，平台不清理其他项目的缓存。

## 日常迭代

本机修改、验证后提交并推送 GitHub，在服务器执行：

```bash
model-test-update
```

自动拉取 main、构建镜像、运行两组离线测试，再检查任务、备份、切换整个 Compose 栈并验证容器健康、HTTPS 和认证。失败自动恢复前一版容器配置和切换前的数据。更新脚本自身也随 Git 版本升级。推送 GitHub 不会立即重启，避免打断正在执行的测试。

切换到指定 Docker 版本：`model-test-update <commit>`。仅接受含 compose.yaml 的提交。代码回退保留当前数据库；若旧代码不支持现有数据库结构，需要停止应用并人工恢复对应完整 data 快照。

```bash
model-test-ctl ps
model-test-ctl logs --tail 100 app
model-test-ctl logs --tail 100 proxy certbot
model-test-ctl restart app
model-test-ctl exec app python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8032/api/health").read().decode())'
docker system df
```

修改登录密码后，更新健康检查使用的 `/root/model-test-login.txt` 中 Password 一行，并重启 proxy：

```bash
htpasswd /etc/model-test/htpasswd admin
chown root:101 /etc/model-test/htpasswd
chmod 640 /etc/model-test/htpasswd
model-test-ctl up -d --force-recreate proxy
```

业务只运行一个 app 容器和一个 Uvicorn worker，避免重复调度测试任务。不应扩容 worker。后端映射的 8032 端口只绑定服务器回环地址；公网入口仍是 HTTPS。

## 验证与开发

更新时自动在临时容器中运行下列离线测试，不挂载生产数据库和凭据、不消耗模型额度：

```bash
python -m unittest -q test_suites
# 工作目录切换到 /app/test-scripts/llm-benchmark 后：
python -m unittest discover -q -s tests
```

本机非容器开发仍可使用 Python 3.12 虚拟环境和 backend/requirements.lock，在 backend 目录运行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8032`。独立验收／图片／视频项目需要对应真实素材；容器不会自动生成缺失的验收材料。

浏览器离线集成验证：安装 Playwright 后，在仓库根目录运行 `node backend/tests/batch_reports_browser.cjs`，可用 TEST_PYTHON 指定 Python，仅调用本地模拟接口。

GitHub 只保存代码、模板和测试，不包含真实凭据、数据库、私人素材和历史结果。
