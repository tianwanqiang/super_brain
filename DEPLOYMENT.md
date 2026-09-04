# 部署到腾讯云

代码这边（Dockerfile / docker-compose.yml / GitHub Actions 工作流 / 登录密码 / 路径可配置化）
都已经准备好了。下面是**只有你能做**的部分——服务器和 GitHub 仓库的密钥我这边没有权限碰。

## 第一步：腾讯云服务器（CVM）

1. 如果还没有服务器：买一台最便宜的 CVM 就够用（这是单用户内部工具，2核4G 以上足够），
   系统选 Ubuntu 22.04 或更新。
2. 装 Docker：
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
   （腾讯云 CVM 内网连 Docker 官方源可能较慢，卡住的话换成阿里云镜像的一键安装脚本）
3. 装 git（Ubuntu 通常自带，没有的话 `apt install -y git`）
4. 生成一对专门给 GitHub Actions 用的部署密钥（不要用你自己电脑上的私钥）：
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/super_brain_deploy -N ""
   cat ~/.ssh/super_brain_deploy.pub >> ~/.ssh/authorized_keys
   cat ~/.ssh/super_brain_deploy   # 私钥内容，下一步要用
   ```
5. 把仓库 clone 到服务器上（第一次手动 clone，之后都是 GitHub Actions 自动 `git pull`）：
   ```bash
   git clone https://github.com/tianwanqiang/super_brain.git /opt/super_brain
   cd /opt/super_brain
   ```
6. 在服务器上手动建 `config.json`（这个文件不会进 git，只存在于服务器本机）：
   ```json
   {
     "DEEPSEEK_API_KEY": "你的真实key",
     "TAVILY_API_KEY": "你的真实key（可选，没有就不用 web_search）",
     "WECHAT_APP_ID": "...",
     "WECHAT_APP_KEY": "...",
     "WECHAT_DEFAULT_COVER_URL": "...",
     "MEETING_MINUTES_DIR": "/opt/super_brain/meeting_minutes",
     "DASHSCOPE_API_KEY": "阿里云百炼的 API Key（sk- 开头，文本向量化用）",
     "DASHSCOPE_WORKSPACE_ID": "阿里云百炼的 WorkspaceId",
     "DASHVECTOR_API_KEY": "DashVector 集群的 API Key（跟 DashScope 是两个独立凭据，别混）",
     "DASHVECTOR_ENDPOINT": "DashVector 集群 endpoint，形如 vrs-xxx.dashvector.cn-xxx.aliyuncs.com"
   }
   ```
   后四个字段是 RAG 检索用的，不配的话 RAG 相关功能不可用，其余功能不受影响。
7. 建一个 `.env` 文件（docker-compose 会自动读取），设登录密码和固定的 session 密钥：
   ```bash
   cat > /opt/super_brain/.env <<'EOF'
   SUPER_BRAIN_PASSWORD=设一个你自己的密码
   SUPER_BRAIN_SECRET_KEY=随便一串足够长的随机字符串
   EOF
   ```
8. 第一次手动启动，确认能跑起来：
   ```bash
   cd /opt/super_brain
   docker compose up -d --build
   docker compose logs -f   # 看有没有报错，Ctrl+C 退出看日志
   ```
9. 云服务器控制台的安全组，放行 5151 端口（或者更推荐：只放行给 nginx 用的 80/443，
   nginx 反向代理到容器的 5151——这样以后套 HTTPS 证书也方便，这一步是可选的加固，
   不装 nginx 直接用 5151 也能跑）。

## 第二步：GitHub 仓库密钥（让 GitHub Actions 能连上服务器）

去 `https://github.com/tianwanqiang/super_brain/settings/secrets/actions`，新增三个 secret：

| Secret 名 | 值 |
|---|---|
| `SERVER_HOST` | 服务器的公网 IP |
| `SERVER_USER` | SSH 登录用户名（比如 `root` 或 `ubuntu`） |
| `SERVER_SSH_KEY` | 第一步生成的**私钥**完整内容（`~/.ssh/super_brain_deploy` 那个文件，不是 `.pub`） |
| `SERVER_DEPLOY_PATH` | 服务器上的仓库路径，比如 `/opt/super_brain` |

## 第三步：验证持续构建

设完密钥之后，随便推一次代码到 `main` 分支（或者去 GitHub 仓库的 Actions 页面手动点
"Run workflow"），观察 Actions 日志——应该会自动 SSH 上服务器、`git pull`、重新构建
镜像并重启容器。

## RAG 功能上线（云端向量化，不占服务器内存）

RAG 检索的向量化/存储/检索全部走阿里云两个云端服务（DashScope 文本向量化 + DashVector
向量数据库），服务器本地不装任何机器学习依赖（`sentence-transformers`/`torch` 已经从
`requirements.txt` 和 `Dockerfile` 里移除）——早期用本地嵌入模型时，这台"最便宜档"
配置（1.9G 内存）真实发生过 gunicorn worker 被系统 OOM Kill、圆桌讨论内容当场丢失的
事故，换成云端方案后这类内存压力从根上不存在了。

前提：`config.json` 里配好上面第 6 步那四个 `DASHSCOPE_*`/`DASHVECTOR_*` 字段。

镜像重新构建好之后，RAG 索引**不会**跟着 `git pull` 自动出现——索引是从 `private.md`
派生出来的构建产物，特意没有进 git（源文件在，索引可以随时重建）。需要手动做一次：

1. 打开 `http://服务器IP:5151/admin`，找到"RAG 检索索引"这一块，点"重建全部专家的 RAG
   索引"。
2. 每次点击都是真实的 DashScope 付费调用（很便宜，text-embedding-v3 约 ¥0.0005/千
   token，且有 50 万 token 免费额度），不需要下载任何模型文件，不受境外直连问题影响。
3. 每个 agent 显示"N 条规则"就是建好了；显示"失败：xxx"多半是 `config.json` 里那
   四个字段没配全，或者 DashVector 集群/API Key 有问题，把报错发给我。

## 之后的日常使用

- 以后要更新代码：本机改完、推送到 GitHub，几分钟内服务器自动更新，不用手动登录服务器。
- 真实数据（`conversations/`、`tasks.yaml`、`config.json`、每个专家的 `lessons.md`）都在
  服务器的 `/opt/super_brain` 目录里，`git pull`/重新构建镜像不会碰这些文件——它们不受
  版本控制管理，容器重启数据也还在（因为整个目录是 volume 挂载进去的，不是打进镜像里的）。
- 忘了密码：SSH 上服务器改 `.env` 里的 `SUPER_BRAIN_PASSWORD`，`docker compose up -d` 重启一下生效。
