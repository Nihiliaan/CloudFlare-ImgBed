#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CloudFlare-ImgBed 自动化部署与多账户多租户管理脚本
功能：
1. 自动校验 Cloudflare API Token 权限与有效性；
2. 自动检测/创建指定账户下的 R2 存储桶；
3. 自动检测/创建指定账户下的 KV 命名空间（img_url）并提取 ID；
4. 自动检测/创建指定账户下的 D1 数据库（img_d1），并全自动执行 database/init.sql 初始化表结构与索引；
5. 自动通过 GitHub CLI (gh) 将 Secrets (含 D1_DATABASE_ID) 注入指定仓库；
6. 自动触发 GitHub Actions 编译部署流水线并监控状态；
7. 支持自定义域名一键绑定至 Worker。
"""

import sys
import os
import json
import argparse
import subprocess
import urllib.request
import urllib.error
import time

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# 加载本地配置文件（若存在）
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_config.json")
_local_config = {}
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _local_config = json.load(f)
    except Exception:
        pass

# 默认全局配置（优先读取本地配置，其次环境变量，最后回退）
DEFAULT_API_TOKEN = _local_config.get("api_token") or os.environ.get("CLOUDFLARE_API_TOKEN", "")
DEFAULT_ACCOUNT_ID = _local_config.get("account_id") or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "afa000613598c8599a2e7e533728c06d")
DEFAULT_BUCKET_NAME = _local_config.get("bucket_name") or "image-server"
DEFAULT_KV_NAME = _local_config.get("kv_name") or "img_url"
DEFAULT_D1_NAME = _local_config.get("d1_name") or "img_d1"
DEFAULT_WORKER_NAME = _local_config.get("worker_name") or "cloudflare-imgbed"
DEFAULT_GITHUB_REPO = _local_config.get("repo") or "Nihiliaan/CloudFlare-ImgBed"
LOCAL_PROXY = _local_config.get("local_proxy") or "http://127.0.0.1:10809"

def get_opener():
    handlers = []
    if LOCAL_PROXY:
        try:
            proxy_support = urllib.request.ProxyHandler({'http': LOCAL_PROXY, 'https': LOCAL_PROXY})
            handlers.append(proxy_support)
        except Exception:
            pass
    return urllib.request.build_opener(*handlers)

def cf_api_request(endpoint, token, method="GET", data=None):
    url = f"https://api.cloudflare.com/client/v4{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "CF-ImgBed-Deployer/2.0"
    }
    encoded_data = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    
    opener = get_opener()
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            parsed = json.loads(error_body)
            return parsed
        except Exception:
            raise RuntimeError(f"Cloudflare API Error HTTP {e.code}: {error_body}")
    except Exception as e:
        raise RuntimeError(f"Cloudflare API Network Error: {e}")

def verify_token(token):
    print("[1/6] 正在校验 Cloudflare API Token 有效性...")
    res = cf_api_request("/user/tokens/verify", token)
    if not res.get("success"):
        errors = res.get("errors", [])
        raise RuntimeError(f"API Token 校验失败: {errors}")
    status = res.get("result", {}).get("status")
    print(f"  [OK] Token 校验通过，状态: {status}")

def ensure_r2_bucket(token, account_id, bucket_name):
    print(f"[2/6] 检查 R2 存储桶 '{bucket_name}' (Account: {account_id})...")
    res = cf_api_request(f"/accounts/{account_id}/r2/buckets", token)
    if not res.get("success"):
        raise RuntimeError(f"获取 R2 存储桶列表失败: {res.get('errors')}")
    
    buckets = [b.get("name") for b in res.get("result", {}).get("buckets", [])]
    if bucket_name in buckets:
        print(f"  [OK] R2 存储桶 '{bucket_name}' 已存在，直接复用。")
    else:
        print(f"  [+] R2 存储桶 '{bucket_name}' 不存在，正在自动创建...")
        create_res = cf_api_request(
            f"/accounts/{account_id}/r2/buckets",
            token,
            method="POST",
            data={"name": bucket_name}
        )
        if not create_res.get("success"):
            raise RuntimeError(f"创建 R2 存储桶失败: {create_res.get('errors')}")
        print(f"  [OK] R2 存储桶 '{bucket_name}' 创建成功！")

def ensure_kv_namespace(token, account_id, kv_name):
    print(f"[3/6] 检查 KV 命名空间 '{kv_name}' (Account: {account_id})...")
    res = cf_api_request(f"/accounts/{account_id}/storage/kv/namespaces", token)
    if not res.get("success"):
        raise RuntimeError(f"获取 KV 命名空间列表失败: {res.get('errors')}")
    
    namespaces = res.get("result", [])
    for ns in namespaces:
        if ns.get("title") == kv_name:
            kv_id = ns.get("id")
            print(f"  [OK] KV 命名空间 '{kv_name}' 已存在，ID: {kv_id}")
            return kv_id
    
    print(f"  [+] KV 命名空间 '{kv_name}' 不存在，正在自动创建...")
    create_res = cf_api_request(
        f"/accounts/{account_id}/storage/kv/namespaces",
        token,
        method="POST",
        data={"title": kv_name}
    )
    if not create_res.get("success"):
        raise RuntimeError(f"创建 KV 命名空间失败: {create_res.get('errors')}")
    kv_id = create_res.get("result", {}).get("id")
    print(f"  [OK] KV 命名空间 '{kv_name}' 创建成功，ID: {kv_id}")
    return kv_id

def ensure_d1_database(token, account_id, d1_name):
    print(f"[4/6] 检查 D1 强一致性关系型数据库 '{d1_name}' (Account: {account_id})...")
    res = cf_api_request(f"/accounts/{account_id}/d1/database", token)
    if not res.get("success"):
        errors = res.get("errors", [])
        if any(e.get("code") == 10000 for e in errors):
            raise RuntimeError(
                "\n[!] Cloudflare API 权限不足 (HTTP 401 Unauthorized):\n"
                "    当前使用的 API Token 缺少 D1 数据库权限！\n"
                "    请登录 Cloudflare 控制台 -> '我的个人资料' (右上角头像) -> 'API 令牌'，\n"
                "    编辑当前 Token，添加权限：'账户 (Account) -> D1 -> 编辑 (Edit)' 后重新运行！"
            )
        raise RuntimeError(f"获取 D1 数据库列表失败: {errors}")
    
    databases = res.get("result", [])
    database_id = None
    for db in databases:
        if db.get("name") == d1_name:
            database_id = db.get("uuid")
            print(f"  [OK] D1 数据库 '{d1_name}' 已存在，UUID: {database_id}")
            break
            
    if not database_id:
        print(f"  [+] D1 数据库 '{d1_name}' 不存在，正在自动创建...")
        create_res = cf_api_request(
            f"/accounts/{account_id}/d1/database",
            token,
            method="POST",
            data={"name": d1_name}
        )
        if not create_res.get("success"):
            raise RuntimeError(f"创建 D1 数据库失败: {create_res.get('errors')}")
        database_id = create_res.get("result", {}).get("uuid")
        print(f"  [OK] D1 数据库 '{d1_name}' 创建成功，UUID: {database_id}")

    # 读取 database/init.sql 并自动初始化表架构与索引
    init_sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database", "init.sql")
    if os.path.exists(init_sql_path):
        print(f"  [*] 正在执行 D1 架构初始化 (database/init.sql)...")
        with open(init_sql_path, "r", encoding="utf-8") as f:
            sql_content = f.read()
            
        query_res = cf_api_request(
            f"/accounts/{account_id}/d1/database/{database_id}/query",
            token,
            method="POST",
            data={"sql": sql_content}
        )
        if not query_res.get("success"):
            print(f"  [!] D1 初始化 SQL 执行返回警告: {query_res.get('errors')}")
        else:
            print(f"  [OK] D1 数据表与索引结构校验/初始化完成！")
    else:
        print(f"  [!] 未找到 database/init.sql 脚本，跳过自动建表。")

    return database_id

def set_github_secret(repo, name, value):
    env = os.environ.copy()
    if LOCAL_PROXY:
        env["HTTP_PROXY"] = LOCAL_PROXY
        env["HTTPS_PROXY"] = LOCAL_PROXY
    cmd = ["gh", "secret", "set", name, "-R", repo, "--body", value]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"设置 GitHub Secret '{name}' 失败: {result.stderr.strip()}")
    print(f"  [OK] GitHub Secret '{name}' 同步成功")

def sync_secrets_to_github(repo, token, account_id, kv_id, bucket_name, worker_name, d1_id=None):
    print(f"[5/6] 正在同步 Secrets 到 GitHub 仓库 '{repo}'...")
    set_github_secret(repo, "CLOUDFLARE_API_TOKEN", token)
    set_github_secret(repo, "CLOUDFLARE_ACCOUNT_ID", account_id)
    if kv_id:
        set_github_secret(repo, "KV_NAMESPACE_ID", kv_id)
    if d1_id:
        set_github_secret(repo, "D1_DATABASE_ID", d1_id)
    set_github_secret(repo, "R2_BUCKET_NAME", bucket_name)
    set_github_secret(repo, "WORKER_NAME", worker_name)
    print("  [OK] 所有 Secrets 已安全注入 GitHub Actions。")

def ensure_github_actions_enabled(repo):
    print(f"  正在确保仓库 '{repo}' 的 GitHub Actions 已启用...")
    env = os.environ.copy()
    if LOCAL_PROXY:
        env["HTTP_PROXY"] = LOCAL_PROXY
        env["HTTPS_PROXY"] = LOCAL_PROXY
    cmd = ["gh", "api", "-X", "PUT", f"/repos/{repo}/actions/permissions", "-F", "enabled=true", "-F", "allowed_actions=all"]
    subprocess.run(cmd, env=env, capture_output=True)

def trigger_and_watch_workflow(repo, worker_name):
    print(f"[6/6] 正在触发 GitHub Actions 部署工作流并监控执行...")
    env = os.environ.copy()
    if LOCAL_PROXY:
        env["HTTP_PROXY"] = LOCAL_PROXY
        env["HTTPS_PROXY"] = LOCAL_PROXY
    
    ensure_github_actions_enabled(repo)
    
    # 获取 Deploy to Cloudflare Workers 的 ID
    list_cmd = ["gh", "workflow", "list", "--all", "-R", repo, "--json", "id,name,state"]
    list_res = subprocess.run(list_cmd, env=env, capture_output=True, text=True)
    workflow_id = "deploy-worker.yml"
    if list_res.returncode == 0 and list_res.stdout.strip():
        try:
            wfs = json.loads(list_res.stdout.strip())
            for w in wfs:
                if "Deploy to Cloudflare Workers" in w.get("name", ""):
                    workflow_id = str(w.get("id"))
                    break
        except Exception:
            pass
            
    print(f"  目标工作流标识: {workflow_id}")
    trigger_cmd = ["gh", "workflow", "run", workflow_id, "-R", repo]
    res = subprocess.run(trigger_cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"触发 Workflow 失败: {res.stderr.strip()}")
    print("  [OK] 已成功触发 'Deploy to Cloudflare Workers' 工作流！")
    
    print("  等待 GitHub Actions 调度启动 (约 5 秒)...")
    time.sleep(5)
    
    # 查找最新的 run
    runs_cmd = ["gh", "run", "list", "--workflow=deploy-worker.yml", "-R", repo, "--json", "databaseId,status,conclusion,url", "-L", "1"]
    runs_res = subprocess.run(runs_cmd, env=env, capture_output=True, text=True)
    if runs_res.returncode == 0 and runs_res.stdout.strip():
        runs = json.loads(runs_res.stdout.strip())
        if runs:
            run_id = runs[0].get("databaseId")
            run_url = runs[0].get("url")
            print(f"  [OK] 任务已入队，Run ID: {run_id}")
            print(f"  查看实时日志: {run_url}")
            print("  正在实时跟踪运行进度...")
            
            watch_cmd = ["gh", "run", "watch", str(run_id), "-R", repo]
            subprocess.run(watch_cmd, env=env)
            
            # 查看最终结论
            view_cmd = ["gh", "run", "view", str(run_id), "-R", repo, "--json", "conclusion"]
            v_res = subprocess.run(view_cmd, env=env, capture_output=True, text=True)
            if v_res.returncode == 0:
                conclusion = json.loads(v_res.stdout.strip()).get("conclusion")
                if conclusion == "success":
                    print("\n==========================================")
                    print("  部署圆满成功！")
                    print(f"  Worker 名称: {worker_name}")
                    print("  访问地址: https://dash.cloudflare.com/ 进入 Workers 即可查看或绑定自定义域名")
                    print("==========================================")
                    return
                else:
                    print(f"\n[!] 部署结束，状态为: {conclusion}，请访问上述日志链接排查。")
                    return

def bind_worker_custom_domain(token, account_id, worker_name, custom_domain):
    if not custom_domain:
        return
    print(f"[*] 正在为 Worker 绑定自定义域名 '{custom_domain}'...")
    parts = custom_domain.split('.')
    possible_zones = ['.'.join(parts[i:]) for i in range(1, max(1, len(parts)-1))]
    possible_zones.append(custom_domain)
    
    zone_id = None
    for z_name in possible_zones:
        try:
            res = cf_api_request(f"/zones?name={z_name}", token)
            if res.get("success") and res.get("result"):
                zone_id = res["result"][0]["id"]
                break
        except Exception:
            continue
            
    if not zone_id:
        print(f"  [-] 未能在当前 Cloudflare 账户中匹配到 '{custom_domain}' 所在的 DNS 区域 (Zone)。")
        return
        
    payload = {
        "hostname": custom_domain,
        "zone_id": zone_id,
        "service": worker_name,
        "environment": "production"
    }
    try:
        res = cf_api_request(f"/accounts/{account_id}/workers/domains", token, method="PUT", data=payload)
        if res.get("success"):
            print(f"  [OK] 自定义域名 '{custom_domain}' 成功绑定至 Worker '{worker_name}'！")
        else:
            errors = res.get("errors", [])
            print(f"  [!] 自定义域名绑定返回异常: {errors}")
    except Exception as e:
        print(f"  [!] 绑定自定义域名网络调用失败: {e}")

def main():
    parser = argparse.ArgumentParser(description="CloudFlare-ImgBed 自动化一键部署与多账号管理器")
    parser.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID, help="Cloudflare 目标 Account ID（默认当前账号）")
    parser.add_argument("--api-token", default=DEFAULT_API_TOKEN, help="Cloudflare API Token（需包含 Workers/D1/KV/R2 编辑权限）")
    parser.add_argument("--bucket-name", default=DEFAULT_BUCKET_NAME, help="R2 存储桶名称（默认 image-server）")
    parser.add_argument("--kv-name", default=DEFAULT_KV_NAME, help="KV 命名空间名称（默认 img_url）")
    parser.add_argument("--d1-name", default=DEFAULT_D1_NAME, help="D1 数据库名称（默认 img_d1）")
    parser.add_argument("--skip-d1", action="store_true", help="跳过 D1 配置，仅使用 KV")
    parser.add_argument("--worker-name", default=DEFAULT_WORKER_NAME, help="Worker 部署服务名称（默认 cloudflare-imgbed）")
    parser.add_argument("--repo", default=DEFAULT_GITHUB_REPO, help="GitHub 部署仓库（默认 Nihiliaan/CloudFlare-ImgBed）")
    parser.add_argument("--custom-domain", default=None, help="可选自定义域名（如 img.nihil.de5.net），若提供将自动绑定")
    
    args = parser.parse_args()
    
    print("==================================================")
    print(" CloudFlare-ImgBed 自动化部署引擎启动 (支持 D1 强一致性)")
    print(f" 目标 Cloudflare 账户: {args.account_id}")
    print(f" 目标存储桶: {args.bucket_name}")
    print(f" 目标 KV 空间: {args.kv_name}")
    if not args.skip_d1:
        print(f" 目标 D1 数据库: {args.d1_name}")
    print(f" 目标 GitHub 仓库: {args.repo}")
    if args.custom_domain:
        print(f" 目标自定义域: {args.custom_domain}")
    print("==================================================")
    
    verify_token(args.api_token)
    ensure_r2_bucket(args.api_token, args.account_id, args.bucket_name)
    kv_id = ensure_kv_namespace(args.api_token, args.account_id, args.kv_name)
    
    d1_id = None
    if not args.skip_d1:
        d1_id = ensure_d1_database(args.api_token, args.account_id, args.d1_name)
        
    sync_secrets_to_github(args.repo, args.api_token, args.account_id, kv_id, args.bucket_name, args.worker_name, d1_id=d1_id)
    trigger_and_watch_workflow(args.repo, args.worker_name)
    if args.custom_domain:
        bind_worker_custom_domain(args.api_token, args.account_id, args.worker_name, args.custom_domain)

if __name__ == "__main__":
    main()
