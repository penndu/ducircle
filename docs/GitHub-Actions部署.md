# GitHub Actions 自动部署

服务器内存只有 1.7G，在上面 `docker build` 容易 OOM。
所以构建放在 GitHub Actions，产物推到 Docker Hub，服务器只负责拉取。

顺带得到一个保证：**构建不过就不会部署**，坏版本上不了线。

---

## 一次性配置

### 1. Docker Hub 准备

**创建仓库**：<https://hub.docker.com/repositories/> → Create repository
→ 名字填 `ducircle`，可见性选 Public

**创建 Access Token**（不是登录密码）：

1. 打开 <https://hub.docker.com/settings/access-tokens>
2. Account settings → Personal access tokens → Generate new token
3. 勾选 `Read & Write`（或 `Read only`，见下方说明）
4. 生成后**立刻复制**——只显示一次

> **建议勾 Read & Write 还是 Read Only？**
> 构建推镜像需要 Write；服务器 pull 是匿名公开拉取，不需要 Token。
> 如果你打算只让 CI 推、服务器匿名拉，用 Read & Write 即可。
> 最小权限原则可用 Write，但不要勾其他无关权限。

### 2. 配置 Actions Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| 名称 | 值 |
|---|---|
| `DOCKERHUB_USERNAME` | `penndu` |
| `DOCKERHUB_TOKEN` | 上一步复制的 Access Token |
| `SERVER_HOST` | `www.dusays.com` |
| `SERVER_USER` | `root` |
| `SERVER_SSH_KEY` | 见下一节 |

---

## ⚠️ 关于 SSH 私钥的安全性

把本机 `~/.ssh/id_ed25519` 的私钥放进 GitHub Secrets 有泄露风险——
一旦泄露，攻击者能用你的身份登进这台服务器（root 权限）。
这台机器上还跑着 artalk、memos、phpmyadmin 等服务。

**推荐做法：生成一对专用于部署的密钥。**

### 在服务器上（先做）

```bash
ssh dusays 'mkdir -p ~/.ssh && ssh-keygen -t ed25519 -f ~/.ssh/github_actions -N "" -C "github-actions-deploy"'
```

### 在 GitHub 上添加 deploy key

**Settings → Deploy keys → Add deploy key** → Title 填 `github-actions` → Add key

粘贴服务器上这个命令的输出：

```bash
ssh dusays 'cat ~/.ssh/github_actions.pub'
```

> ⚠️ **不要勾 "Allow write access"** —— 部署只需要拉代码。
> 服务器上的代码目录由 Actions 手动 `git pull`，不需要 push 权限。

### 取私钥内容填进 Secrets

```bash
ssh dusays 'cat ~/.ssh/github_actions'
```

把输出粘到 `SERVER_SSH_KEY`。

这样即使这个私钥泄露，攻击者只能拉代码，不能登进服务器。

### 部署完后可以清掉服务器上的私钥

Actions 每次部署都会重新写入 authorized_keys（appleboy/ssh-action 的行为），
所以服务器上 `/root/.ssh/github_actions` 用完可以删：

```bash
ssh dusays 'rm -f ~/.ssh/github_actions'
```

---

## 3. 服务器上的 `.env`

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index

# 首次：克隆代码
git clone git@github.com:penndu/ducircle.git .

# 生成 .env
cp .env.example .env
echo "" >> .env
echo "SECRET_KEY=$(openssl rand -base64 48)" >> .env
chmod 600 .env
```

填入数据库与 Redis 密码（见部署清单第三节）。

## 4. 容器接入网络

**这一步 1Panel 用户最容易漏。** 在 1Panel「容器」页面，
给应用容器加入 MySQL 和 Redis 所在的网络：

- `1Panel-mysql-wQr4` 所属网络
- `1Panel-redis-29uO` 所属网络

验证：

```bash
docker exec ducircle getent hosts 1Panel-mysql-wQr4
```

---

## 首次部署

推代码触发 Actions：

```bash
git commit --allow-empty -m "触发首次构建"
git push
```

看进度：<https://github.com/penndu/ducircle/actions>

Actions 跑完会自动 SSH 到服务器完成部署。

### 手动跑一次（不想等 CI）

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
docker logs -f ducircle
```

---

## 日常更新

配好后**只要 push 到 main**：

```bash
git commit -am "改动说明"
git push
```

Actions 自动：静态检查 → 跑测试 → 构建双架构镜像 → 推 Docker Hub
→ SSH 到服务器 pull → 重启 → 健康检查。

看进度：<https://github.com/penndu/ducircle/actions>

### 部署失败时

Actions 会失败并把服务器上的日志带回来。常见原因：

| 现象 | 原因 |
|---|---|
| `No module named pymysql` | 依赖缺失（已修过，加新依赖时注意） |
| 迁移失败，容器起不来 | 数据库连接问题，检查 `.env` 与容器网络 |
| 健康检查失败 | 应用起来了但 /health 返回非 200，看日志里的数据库状态 |
| `no space left on device` | 磁盘满了，`docker system prune -a` 清理 |

---

## 镜像标签

| 标签 | 用途 |
|---|---|
| `latest` | main 分支最新 |
| `sha-<短哈希>` | 精确回滚 |
| `main` | 分支名（与 latest 同步） |

**回滚**：

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index
# 改 compose 里的 image 为 sha-xxxxxxx
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
```

---

## CI 里跑了什么

推送镜像前会执行：

1. **await 位置检查** —— 防止 `await db.execute(q).scalar_one()` 这类错误
   （`.scalar_one()` 会作用在协程上，报错信息完全指不到真因）
2. **MySQL 方言兼容性检查** —— 索引长度、外键、租户表索引覆盖
3. **单元测试** —— 277 个用例
4. **双架构构建** —— linux/amd64 + linux/arm64，换服务器不用改 CI

测试不过，镜像不推，服务器不更新。