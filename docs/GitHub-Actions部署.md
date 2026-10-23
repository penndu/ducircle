# GitHub Actions 自动部署

服务器内存只有 1.7G，在上面 `docker build` 容易 OOM。
所以构建放在 GitHub Actions，产物推到 ghcr.io，服务器只负责拉取。

顺带得到一个保证：**构建不过就不会部署**，坏版本上不了线。

---

## 一次性配置

### 1. GitHub 仓库

仓库地址：<https://github.com/penndu/ducircle>

本地已配置好远程推送，`git push` 即可。

### 2. 开启 GHCR 匿名拉取

Workflow 用 `GITHUB_TOKEN` 推包，但服务器是**匿名拉取**，必须把包设为 public：

1. 打开 <https://github.com/penndu/ducircle/packages>
2. 首次推送后这里会出现 `ducircle` 包
3. 进��� → Package settings → Change visibility → Public

不改的话服务器 `docker pull` 会报 403。

### 3. 配置部署用的 Secrets

只有**自动部署**需要（如果你打算手动 SSH 部署，可以跳过）：

**Settings → Secrets and variables → Actions → New repository secret**

| 名称 | 值 |
|---|---|
| `SERVER_HOST` | `www.dusays.com` |
| `SERVER_USER` | `root` |
| `SERVER_SSH_KEY` | 本机 `~/.ssh/id_ed25519` 的**私钥内容** |

取私钥内容：

```bash
cat ~/.ssh/id_ed25519
```

> ⚠️ 私钥进 GitHub Secrets 有泄露风险。更稳的做法是用 deploy key
> （只读）或改成手动部署。如果这台机器上有其他服务，建议用专门的部署密钥。

### 4. 服务器上的 `.env`

```bash
cd /opt/1panel/www/sites/ducircle.dusays.com/index
cp .env.example .env
echo "SECRET_KEY=$(openssl rand -base64 48)" >> .env
chmod 600 .env
```

填入数据库与 Redis 密码（见部署清单第三节）。

### 5. 容器接入网络

**这一步 1Panel 用户最容易漏。** 在 1Panel「容器」页面，
给应用容器加入 MySQL 和 Redis 所在的网络：

- `1Panel-mysql-wQr4` 所属网络
- `1Panel-redis-29uO` 所属网络

验证：

```bash
docker exec ducircle getent hosts 1Panel-mysql-wQr4
```

---

## 首次部署（手动）

Actions 还没跑过时先手动来一次：

```bash
cd /opt/1panel/www/sites/ducircle.dusays.com/index

git pull origin main
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
docker logs -f ducircle
```

日志应出现：

```
数据库连接正常
Redis 连接正常
调度器已启动，任务：定时抓取, 过期数据清理, 统计快照, AI 摘要生成
```

---

## 之后的日常更新

配好 Secrets 后，**只要 push 到 main 就完��**：

```bash
git commit -am "改动说明"
git push
```

Actions 会自动：跑测试 → 构建镜像 → 推 GHCR → SSH 到服务器 pull 并重启。

看进度：<https://github.com/penndu/ducircle/actions>

### 想跳过测试直接部署

不建议 —— 那些测试抓到过 3 个真 bug，其中一个是租户隔离的安全缺口。

### 手动触发一次

Actions 页面 → Build and push image → Run workflow。

改了 Dockerfile 或想重跑构建时用。

---

## 镜像标签

| 标签 | 用途 |
|---|---|
| `latest` | main 分支最新 |
| `sha-<短哈希>` | 精确回滚到某个版本 |
| `main` | 分支名（与 latest 同步） |

**回滚**：

```bash
docker compose -f deploy/docker-compose.yml pull
# 改 compose 里的 image 为 sha-xxxxxxx
docker compose -f deploy/docker-compose.yml up -d
```

---

## CI 里跑了什么

Workflow 在推送镜像前会执行：

1. **await 位置检查** —— 防止 `await db.execute(q).scalar_one()` 这类错误
   （`.scalar_one()` 会作用在协程上，报错信息完全指不到真因）
2. **单元测试** —— 277 个用例
3. **多架构构建** —— linux/amd64 + linux/arm64，
   换服务器不用重新配 CI

测试不过，镜像不会推，服务器不会更新。