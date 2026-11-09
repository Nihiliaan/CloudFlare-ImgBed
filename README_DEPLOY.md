# CloudFlare-ImgBed 自动化多账户部署与运维指南

本项目提供了一套全自动化的 Cloudflare ImgBed（三月图床）多租户部署脚本 `deploy.py`。无论是部署您自己的账户，还是未来为朋友跨账号部署（朋友使用自己的 Cloudflare 免费配额），均可在终端通过单条命令完成全套资源的自动供给与上线。

---

## 一、 底层架构与多租户运行机理

### 1. 资源拓扑与计费隔离
- **前端与后端 API**：编译打包为单一 Serverless Worker，托管于 Cloudflare 全球边缘网络；
- **文件存储 (R2)**：存储图片物理文件的对象存储桶，每个账户享有每月 10GB 的永久免费存储与充足的读写配额；
- **元数据数据库 (Workers KV)**：负责存储图片哈希、索引列表、管理员密码及系统设置；
- **多账户隔离机制**：
  朋友注册 Cloudflare 账号后，只需在 Cloudflare 控制台的「管理帐户」→「成员」中，将您的 Cloudflare 邮箱（`1601904149@qq.com`）添加为管理员（Administrator）。
  此后，您现有的 API 令牌即天然具备管理对方账户资源的合法权限。部署到对方 `Account ID` 下的所有计算与存储资源，**全额计入对方的免费账单，零消耗您的额度**。

---

## 二、 自动化部署脚本功能说明 (`deploy.py`)

脚本基于 Cloudflare REST API v4 与 GitHub CLI 编排，实现全链路零人工干预：
1. **API Token 鉴权校验**：调用 `/user/tokens/verify` 确认凭证有效性；
2. **R2 存储桶自愈与供给**：调用 `/accounts/{id}/r2/buckets`，若目标账户缺少指定存储桶则自动创建；
3. **KV 命名空间自愈与供给**：调用 `/accounts/{id}/storage/kv/namespaces` 检索 `img_url`，缺失时自动创建并提取生成的唯一命名空间 ID；
4. **CI/CD 凭据热注入**：调用 `gh secret set` 动态将目标账户的凭据写入 GitHub Actions Secrets；
5. **云端流水线触发与监控**：通过 GitHub Actions 自动调用 Node.js 22 环境进行代码分析、路由生成与 Wrangler 边缘部署，并由脚本实时监听返回部署结果。

---

## 三、 为朋友部署的极简实操步骤（未来复用）

未来当有新朋友需要部署时，仅需 2 步：

### 第一步：让朋友给您授权（耗时约 30 秒）
1. 朋友登录其 Cloudflare 仪表盘，进入「管理帐户」→「成员」；
2. 点击「邀请成员」，输入您的邮箱 `1601904149@qq.com`，角色选择 **管理员 (Administrator)** 发送邀请；
3. 您在邮箱中点击确认接受邀请后，在控制台首页或左侧即可看到对方的 **账户 ID (Account ID)**。

### 第二步：本地终端一键执行部署
在终端（或全局 Conda 环境 `Python3.14.3`）下直接运行命令：
```bash
python deploy.py --account-id <朋友的Account_ID> --worker-name <朋友自定义的Worker名> [--custom-domain <朋友绑定的域名>]
```
*例如：*
```bash
python deploy.py --account-id b1234567890abcdef1234567890abcde --worker-name friend-imgbed --custom-domain img.friend.com
```
脚本将全自动在朋友账号下完成：
- 校验并创建存储桶 `image-server`；
- 校验并创建 KV 空间 `img_url`；
- 自动为 Worker 绑定自定义域名（若提供）；
- 打包并发布至朋友专属域名或 `https://friend-imgbed.<朋友子域名>.workers.dev`。

---

## 四、 首次访问与后台配置（必做）

部署成功后，首次进入图床后台开启存储渠道：
1. 浏览器访问部署完成的 Worker 域名（如已打开的页面：`https://cloudflare-imgbed.1601904149.workers.dev/`）；
2. 根据页面提示设置管理员账号与密码；
3. 进入系统后台：「系统设置」→「上传设置」；
4. 勾选并开启 **CF R2 存储** 选项保存即可。
