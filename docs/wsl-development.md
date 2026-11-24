# WSL2 开发与测试

本项目的主要开发副本位于 WSL2 发行版 `loss-repricing`：

- Linux 路径：`/home/developer/projects/loss-repricing`
- Windows 文件管理器路径：`\\wsl$\loss-repricing\home\developer\projects\loss-repricing`
- 运行环境：Ubuntu 24.04、Python 3.12、Node.js 22。
- Windows 原目录 `D:\projects\loss-repricing` 保留为迁移前副本；后续修改和 Git 操作在 WSL 副本进行。

## 进入项目

在 PowerShell 中执行：

```powershell
wsl -d loss-repricing -u developer --cd /home/developer/projects/loss-repricing
```

如果使用支持 WSL 的编辑器，在该发行版连接并打开 Linux 项目目录。

## 安装与验证

以下命令在 WSL 中执行：

```bash
cd /home/developer/projects/loss-repricing
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
npm ci
npm run build
cd ..
.venv/bin/python -m pytest
```

遵守先前端构建、后后端测试的顺序；依赖已有时不必每次重装。当前后端运行依赖使用 `backend/requirements.txt`；Windows 导出的 `requirements-local-lock.txt` 仅作历史记录，不直接视作 Linux 依赖锁。

## 启动与停止

```bash
bash deploy/start-wsl.sh
```

该命令在前台运行 API 和 worker，访问 `http://127.0.0.1:18765`，按 `Ctrl+C` 同时停止两者。本地启动入口要求关闭真实提交。管理员密码仍在项目 `.env` 中本机查看，勿提交到 Git。

迁移保留本项目 `.env`、Git 历史、未提交文件及运行资料；SQLite 数据库通过备份接口复制，未复制 Windows 虚拟环境、node_modules、构建产物和缓存。迁移后的 demo 数据库是独立副本，后续不会与 Windows 副本自动同步。

## 与服务器同步

在 WSL 中检查差异、提交并推送代码；服务器按确认的 Git 提交部署。迁入 WSL 不代表已配置服务器地址、仓库认证或自动部署。`.env`、真实合同、原始业务资料和数据库仍不入 Git。上线应单独验证服务器依赖、数据库迁移与实际运行结果。