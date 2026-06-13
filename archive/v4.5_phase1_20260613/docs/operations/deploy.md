# 部署指南

## Git 工作流

本地开发 → `git commit` → `git push` → 服务器 `pull` → `bash deploy.sh`

## 常用命令

```bash
# 一键完整部署
ssh root@8.210.3.197 "cd /root/BitgetBot && git pull && bash deploy.sh"

# 仅重启 (不拉代码)
ssh root@8.210.3.197 "cd /root/BitgetBot && bash deploy.sh"

# 查看运行状态
ssh root@8.210.3.197 "ps aux | grep deepseek_quant | grep -v grep"

# 查看实时日志
ssh root@8.210.3.197 "tail -30 /tmp/bot.log"
```

## 回滚

```bash
ssh root@8.210.3.197
cd /root/BitgetBot
git log --oneline -5                # 找到目标 commit
git reset --hard <commit>           # 回退版本
bash deploy.sh                      # 重启
```

## 备用方案 (GitHub 不可用时)

```powershell
.\sync.ps1          # MD5 对比 → SCP 上传变更 → 远程重启
.\sync.ps1 -DryRun  # 仅预览差异，不上传
```

## SSH 密钥路径

Windows 中文用户名环境下需显式指定密钥路径:
```bash
SSH_KEY=/c/Users/林华俊/.ssh/id_ed25519
KNOWN_HOSTS=/tmp/ssh_known_hosts
```

> 相关文件: `deploy.sh`, `start.sh`, `sync.ps1`, `.env`
