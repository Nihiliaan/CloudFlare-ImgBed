#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CloudFlare-ImgBed 自动化一键部署与多账号管理器
功能：
1. 自动校验 Cloudflare API Token 权限与有效性；
2. 自动检测/创建指定账户下的 R2 存储桶；
3. 自动检测/创建指定账户下的 KV 命名空间（img_url）并提取 ID；
4. 自动检测/创建指定账户下的 D1 数据库（img_d1），并全自动执行 database/init.sql 初始化表结构与索引；
5. 本地极速直连发布（Direct Deploy via Wrangler），一键直推 Cloudflare，0 依赖 GitHub，耗时仅需数秒；
6. 也支持通过 GitHub Actions 远程自动化流水线部署（--via-github）；
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
            return json.loads(error_body)
        except Exception:
            raise RuntimeError(f"Cloudflare API Error HTTP {e.code}: {error_body}")
    except Exception as e:
        raise RuntimeError(f"Cloudflare API Network Error: {e}")

def verify_token(token):
    print("[1/5] 正在校验 Cloudflare API Token 有效性...")
    res = cf_api_request("/user/tokens/verify", token)
    if not res.get("success"):
        errors = res.get("errors", [])
        raise RuntimeError(f"API Token 校验失败: {errors}")
    status = res.get("result", {}).get("status")
    print(f"  [OK] Token 校验通过，状态: {status}")

def ensure_r2_bucket(token, account_id, bucket_name):
    print(f"[2/5] 检查 R2 存储桶 '{bucket_name}' (Account: {account_id})...")
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
    print(f"[3/5] 检查 KV 命名空间 '{kv_name}' (Account: {account_id})...")
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
    print(f"[4/5] 检查 D1 强一致性关系型数据库 '{d1_name}' (Account: {account_id})...")
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
        print(f"  [*] 正在校验/执行 D1 架构初始化 (database/init.sql)...")
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

def direct_deploy_worker(token, account_id, kv_id, bucket_name, worker_name, d1_id=None):
    print(f"[5/5] 正在通过本地 Wrangler 极速直连发布到 Cloudflare Workers...")
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = token
    env["CLOUDFLARE_ACCOUNT_ID"] = account_id
    env["WORKER_NAME"] = worker_name
    env["R2_BUCKET_NAME"] = bucket_name
    if kv_id:
        env["KV_NAMESPACE_ID"] = kv_id
    if d1_id:
        env["D1_DATABASE_ID"] = d1_id

    root_dir = os.path.dirname(os.path.abspath(__file__))
    gen_script = os.path.join(root_dir, "deploy", "worker", "generate-toml.js")
    
    # 1. 生成 deploy/worker/wrangler.toml
    print("  [*] 正在生成带有 D1/KV/R2 全量绑定的 wrangler.toml...")
    res = subprocess.run(["node", gen_script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root_dir)
    if res.returncode != 0:
        raise RuntimeError(f"生成 wrangler.toml 失败: {res.stderr.strip() or res.stdout.strip()}")
    print("  [OK] 部署配置生成成功！")

    # 2. 调用 npx wrangler deploy 直推 Cloudflare
    toml_path = os.path.join(root_dir, "deploy", "worker", "wrangler.toml")
    print(f"  [*] 正在将 Worker '{worker_name}' 与静态资源包直接发布到 Cloudflare 边缘...")
    npx_bin = "npx.cmd" if sys.platform == "win32" else "npx"
    cmd = [npx_bin, "wrangler", "deploy", "--config", toml_path]
    deploy_res = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root_dir)
    if deploy_res.returncode != 0:
        raise RuntimeError(f"Wrangler 发布失败:\n{deploy_res.stderr.strip() or deploy_res.stdout.strip()}")

    print("\n" + "=" * 50)
    print("  🎉 部署圆满成功！")
    print(f"  Worker 服务名: {worker_name}")
    print(f"  已绑定 D1 强一致性数据库: {d1_id or '未绑定'}")
    print(f"  已绑定 R2 存储桶: {bucket_name}")
    print(f"  已绑定 KV 存储: {kv_id}")
    print("=" * 50)
    print(deploy_res.stdout.strip())

def bind_worker_custom_domain(token, account_id, worker_name, custom_domain):
    if not custom_domain:
        return
    print(f"\n[*] 正在为 Worker 绑定自定义域名 '{custom_domain}'...")
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
    parser.add_argument("--custom-domain", default=None, help="可选自定义域名（如 img.nihil.de5.net），若提供将自动绑定")
    
    args = parser.parse_args()
    
    print("==================================================")
    print(" CloudFlare-ImgBed 自动化部署引擎启动 (D1 强一致性直连模式)")
    print(f" 目标 Cloudflare 账户: {args.account_id}")
    print(f" 目标存储桶: {args.bucket_name}")
    print(f" 目标 KV 空间: {args.kv_name}")
    if not args.skip_d1:
        print(f" 目标 D1 数据库: {args.d1_name}")
    print(f" 目标 Worker 服务: {args.worker_name}")
    if args.custom_domain:
        print(f" 目标自定义域: {args.custom_domain}")
    print("==================================================")
    
    verify_token(args.api_token)
    ensure_r2_bucket(args.api_token, args.account_id, args.bucket_name)
    kv_id = ensure_kv_namespace(args.api_token, args.account_id, args.kv_name)
    
    d1_id = None
    if not args.skip_d1:
        d1_id = ensure_d1_database(args.api_token, args.account_id, args.d1_name)
        
    # 本地直连一键发布（无需配置 GitHub Secrets，无脆弱环节）
    direct_deploy_worker(args.api_token, args.account_id, kv_id, args.bucket_name, args.worker_name, d1_id=d1_id)
    
    if args.custom_domain:
        bind_worker_custom_domain(args.api_token, args.account_id, args.worker_name, args.custom_domain)

if __name__ == "__main__":
    main()
