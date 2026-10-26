# GitHub 同步通用记忆

## 推荐原则

优先使用 SSH 方式连接 GitHub。HTTPS 在部分网络环境下可能出现 443 端口连接失败，SSH 通常更稳定，也避免反复处理 HTTPS 凭据问题。

检查当前远端：

```powershell
git remote -v
```

SSH remote 通常长这样：

```text
origin  git@github.com:<owner>/<repo>.git (fetch)
origin  git@github.com:<owner>/<repo>.git (push)
```

HTTPS remote 通常长这样：

```text
origin  https://github.com/<owner>/<repo>.git (fetch)
origin  https://github.com/<owner>/<repo>.git (push)
```

## 将 Remote 改成 SSH

如果当前 remote 是 HTTPS，改成 SSH：

```powershell
git remote set-url origin git@github.com:<owner>/<repo>.git
```

再次确认：

```powershell
git remote -v
```

## SSH 命令写法

在 PowerShell 中，为了避免首次连接 host key 时卡住，可以临时设置：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
```

说明：

- `BatchMode=yes`：避免命令进入交互式密码输入。
- `StrictHostKeyChecking=accept-new`：首次连接自动接受新的 host key，之后复用本机 SSH 配置。

该环境变量只影响当前 PowerShell 会话。

## 日常拉取

推荐先确认工作区状态：

```powershell
git status -sb
```

如果工作区有未提交改动，先判断是否需要提交或暂存。不要随意清理未提交改动。

拉取远端主分支：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git pull --rebase origin main
```

如果仓库默认分支不是 `main`，替换成实际分支名，例如：

```powershell
git pull --rebase origin master
```

只刷新远端状态、不合并代码：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git fetch origin
git status -sb
```

## 日常推送

提交前检查：

```powershell
git status --short --ignored
```

重点确认不要提交：

- 本地 `.env` 文件。
- `raw_data/`、`data/`、`output/` 等真实数据目录。
- `logs/`。
- 浏览器登录状态、cookies、profile。
- 密码、token、授权码、私钥。
- 真实邮件、附件、流水、订单截图、账单 PDF。

暂存、提交、推送：

```powershell
git add .
git commit -m "简短说明本次改动"
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git push origin main
```

如果当前分支不是 `main`：

```powershell
git branch --show-current
git push origin <branch-name>
```

推送后验证：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git fetch origin
git status -sb
```

如果输出类似下面，表示当前分支和远端同步：

```text
## main...origin/main
```

## 临时用 SSH URL 直接推送

如果不想马上修改 `origin`，可以直接推送到 SSH URL：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git push git@github.com:<owner>/<repo>.git <branch-name>
```

确认成功后，再考虑把 `origin` 改成 SSH。

## 常见问题

### HTTPS 连接失败

如果看到：

```text
Failed to connect to github.com port 443
```

先看 remote：

```powershell
git remote -v
```

如果是 HTTPS，切到 SSH：

```powershell
git remote set-url origin git@github.com:<owner>/<repo>.git
```

然后重试：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git push origin <branch-name>
```

### 本地 Ahead

如果 `git status -sb` 显示：

```text
## main...origin/main [ahead N]
```

表示本地有 N 个提交尚未推送。执行：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git push origin main
```

### 本地 Behind

如果显示：

```text
## main...origin/main [behind N]
```

表示远端有 N 个提交本地没有。推荐：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git pull --rebase origin main
```

### Ahead 和 Behind 同时存在

如果显示类似：

```text
## main...origin/main [ahead 1, behind 1]
```

先 rebase 远端：

```powershell
$env:GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
git pull --rebase origin main
```

解决冲突并完成 rebase 后再推送：

```powershell
git push origin main
```

### SSH 权限失败

如果看到：

```text
Permission denied (publickey).
```

说明本机 SSH key 没有被 GitHub 接受。检查：

```powershell
ssh -T git@github.com
```

需要确保：

- 本机有 SSH key。
- 公钥已添加到 GitHub。
- 当前 ssh-agent 或 SSH 配置能找到对应私钥。

### 推送前有本地改动

如果 `git status -sb` 显示有 `M`、`A`、`??`：

- 确认这些改动属于当前任务。
- 确认没有真实隐私数据。
- 再 `git add` 和 `git commit`。

不要使用 `git reset --hard` 或 `git checkout --` 清理改动，除非明确知道这些改动不需要保留。
