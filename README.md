# 模型测试平台

沿用管理平台，整合 22 个测试项目、流式并发曲线、25 项指标与可编辑报告。执行范围支持单个项目、所有项目；真实调用先预览计划并确认。报告支持在线预览、修改、版本记录和 HTML / CSV / JSON 导出。

## 本机运行

需要 Python 3.12+、curl、ps。只运行一个 Uvicorn worker。

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.lock
cd backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8032
```

打开 http://127.0.0.1:8032 。图片、视频默认放在根目录 `media/test.png`、`media/test.mp4`，也可配置 `MT_MEDIA_DIR`、`MT_IMAGE_FILE`、`MT_VIDEO_FILE`。缺少素材时相应项目不能预览执行。独立验收包需要额外的 fixtures，部署材料仍需人工核验。未迁入历史记录和 API 密钥。

## 服务器部署

Ubuntu 24.04，使用 systemd + Nginx。平台以专用普通用户运行，后端仅监听回环地址，公网全部页面与接口经过 HTTPS 和 Nginx 登录认证。

- `/opt/model-test/repository`：GitHub 仓库，root 管理。
- `/opt/model-test/releases/<commit>`：独立代码版本与虚拟环境。
- `/opt/model-test/current`：当前运行版本。
- `/var/lib/model-test/data`：数据库、密钥和测试证据，更新不会覆盖。
- `/var/lib/model-test/media`：本机测试素材，只保存在服务器。
- `/var/lib/model-test/acceptance-fixtures`：独立验收材料，每版软链接复用。
- `/etc/model-test/platform.env`：环境变量，见 deploy/platform.env.example。
- `/etc/model-test/htpasswd`：登录密码哈希；使用 `htpasswd /etc/model-test/htpasswd admin` 修改。
- `/var/backups/model-test`：更新前的完整数据备份，包含 secret.key，权限仅 root 可读。

初次部署（适用于未配置其他站点的 Ubuntu 服务器，80/443 端口需开放）：

```bash
git clone https://github.com/cieyyy/model_test_system.git /opt/model-test/repository
bash /opt/model-test/repository/deploy/install.sh YOUR_SERVER_IP
```

脚本创建系统用户、持久目录和 Nginx 登录认证，为 IPv4 地址申请 Let's Encrypt 短期证书，配置每 6 小时检查续期。初始登录信息仅保存于服务器 `/root/model-test-login.txt`。`model-test-certbot.timer` 负责续期，保持 80 端口可访问。脚本会替换默认 Nginx 站点；已有业务服务器应手工合并配置。

## 日常迭代

在本机此仓库修改代码、测试、提交并推送 GitHub，然后在服务器运行：

```bash
model-test-update
```

脚本拉取 main、安装锁定依赖、运行离线测试，再进入维护状态。有任务执行时拒绝更新；无任务时停止服务，备份数据库、密钥和证据，切换版本并健康检查。失败自动恢复前一版代码和数据。更新脚本自身通过重新安装 `deploy/update.sh` 到 `/usr/local/bin/model-test-update` 升级；systemd/Nginx 配置变更需管理员单独应用。

回到指定已知提交：`model-test-update <commit>`。这是代码切换，会保留当前数据库；跨不兼容结构版本需停止服务并从对应备份恢复整个 data 目录，再启动。备份包含敏感密钥，应保存到受控位置，定期清理旧备份和无用 releases。

```bash
systemctl status model-test
journalctl -u model-test -n 100 --no-pager
```

推送 GitHub 不会自动重启服务器。更新为显式的一条命令，避免打断测试。

## 验证

```bash
cd backend
.venv/bin/python -m unittest -v test_suites
cd ../test-scripts/llm-benchmark
../../backend/.venv/bin/python -m unittest discover -v -s tests
```

浏览器集成验证：安装 Playwright 后，在仓库根目录运行 `node backend/tests/batch_reports_browser.cjs`，可用 TEST_PYTHON 指定 Python。该验证只调用本地模拟接口，不消耗模型额度。

GitHub 仓库只保存代码、模板及测试，不包含真实凭据、运行数据库、私人素材或历史结果。部署环境应始终保留整个 data 目录，丢失 secret.key 将无法解密已保存的 API Key。
