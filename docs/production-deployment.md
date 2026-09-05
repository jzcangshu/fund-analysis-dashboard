# 生产部署记录

本文只记录可公开提交的部署信息，不包含密码、令牌、授权码、私钥内容或生产环境文件。

## 当前环境

- 主机：`8.217.191.193`
- SSH 用户：`root`
- 应用目录：`/opt/fund-dashboard`
- Compose 文件：`deploy/compose.prod.yml`
- 生产数据库：PostgreSQL 16（数据库容器）
- 生产入口：`https://danyintouzi.com`
- SSH 私钥：使用运维人员本机的受保护密钥，通过 SSH（安全远程登录）客户端 `-i` 参数引用；私钥不进入仓库。

## 发布门禁

1. 本地完成后端与工具测试、规范、格式、类型和差异检查。
2. 在生产数据库容器中生成部署前 custom format（自定义格式）备份。
3. 使用 PostgreSQL 16（数据库版本）对应的 `pg_restore`（归档恢复工具）完整读取备份；失败不得迁移或重启。
4. 记录服务器当前提交并创建 `backup/predeploy-<UTC 时间>`（部署前备份分支）用于回滚定位。
5. 构建 API、worker 和 Caddy 镜像，执行 Alembic（数据库迁移工具）`upgrade head`，再启动服务。
6. 核对 API、worker、Caddy、数据库容器健康状态，检查迁移版本、公网存活接口和关键数据计数。

## 当前发布

- Commit（提交）：`332f5b0`（生产代码）
- 部署日期：2026-09-06
- 本地标签：`prod-20260906-332f5b0`
- 服务器迁移版本：`0009_cleanup_settings_schedule (head)`
- 部署前备份：服务器备份卷中的 `database-predeploy-20260905T172045Z.dump`，约 21 MiB（兆字节），已通过完整归档读取验证。

## 常用核对命令

```bash
cd /opt/fund-dashboard
docker compose --env-file deploy/.env -f deploy/compose.prod.yml ps
docker compose --env-file deploy/.env -f deploy/compose.prod.yml exec -T api python -m alembic -c /app/backend/alembic.ini current
curl --fail --silent --show-error https://danyintouzi.com/health/live
```

受保护接口使用会话 Cookie（会话 Cookie）验证；登录响应不返回 Bearer token（令牌）。生产 API 的 8000 端口仅在 Docker 网络内暴露。

## 回滚原则

迁移前保留数据库备份和部署前分支。若新服务未通过健康检查，先停止继续变更并保留现场；无数据库结构变更时可切回上一已验证镜像。已执行新迁移后不要直接运行旧代码，优先使用向前兼容版本或从备份恢复。
