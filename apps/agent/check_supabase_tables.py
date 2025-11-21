#!/usr/bin/env python3
"""
检查 Supabase 数据库可访问的表

使用方法：
    cd apps/agent
    python check_supabase_tables.py
"""

import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

# 加载 .env 文件
load_dotenv()

# 获取环境变量
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# 优先使用 SERVICE_KEY，如果没有则使用 ANON_KEY
SUPABASE_KEY = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY

def check_connection(client: Client):
    """检查连接是否正常"""
    try:
        # 尝试查询一个简单的表（如果存在）
        result = client.table("jobs").select("run_id").limit(1).execute()
        return True, "连接成功"
    except Exception as e:
        error_msg = str(e)
        if "relation" in error_msg.lower() or "does not exist" in error_msg.lower():
            return True, "连接成功，但表不存在"
        return False, f"连接失败: {error_msg}"

def list_tables(client: Client):
    """列出所有可访问的表"""
    # Supabase 通过 REST API 访问，无法直接查询系统表
    # 我们需要尝试访问已知的表，看哪些可以访问
    
    known_tables = [
        "jobs",
        "video_tasks",
        "prompts_library",
        "profiles",
        "users",  # auth.users 需要通过特殊方式访问
    ]
    
    accessible_tables = []
    inaccessible_tables = []
    
    print("\n" + "="*60)
    print("检查可访问的表...")
    print("="*60)
    
    for table_name in known_tables:
        try:
            # 尝试查询表（只查询 1 条记录）
            if table_name == "users":
                # auth.users 需要通过 RPC 或特殊方式访问
                result = client.table("users").select("id").limit(1).execute()
            else:
                result = client.table(table_name).select("*").limit(1).execute()
            
            accessible_tables.append({
                "name": table_name,
                "accessible": True,
                "row_count": None  # 无法直接获取行数
            })
            print(f"✅ {table_name}: 可访问")
        except Exception as e:
            error_msg = str(e)
            if "relation" in error_msg.lower() or "does not exist" in error_msg.lower():
                inaccessible_tables.append({
                    "name": table_name,
                    "accessible": False,
                    "reason": "表不存在"
                })
                print(f"❌ {table_name}: 表不存在")
            elif "permission" in error_msg.lower() or "policy" in error_msg.lower() or "row-level" in error_msg.lower():
                inaccessible_tables.append({
                    "name": table_name,
                    "accessible": False,
                    "reason": "权限不足（RLS 策略限制）"
                })
                print(f"⚠️  {table_name}: 权限不足（RLS 策略限制）")
            else:
                inaccessible_tables.append({
                    "name": table_name,
                    "accessible": False,
                    "reason": f"错误: {error_msg[:100]}"
                })
                print(f"❌ {table_name}: 错误 - {error_msg[:100]}")
    
    return accessible_tables, inaccessible_tables

def get_table_info(client: Client, table_name: str):
    """获取表的详细信息"""
    try:
        # 尝试获取表结构（通过查询空结果集）
        result = client.table(table_name).select("*").limit(0).execute()
        
        # 尝试获取一些示例数据
        sample = client.table(table_name).select("*").limit(5).execute()
        
        return {
            "exists": True,
            "sample_count": len(sample.data) if sample.data else 0,
            "columns": list(sample.data[0].keys()) if sample.data and len(sample.data) > 0 else []
        }
    except Exception as e:
        return {
            "exists": False,
            "error": str(e)
        }

def main():
    print("="*60)
    print("Supabase 数据库表检查工具")
    print("="*60)
    
    # 检查环境变量
    if not SUPABASE_URL:
        print("❌ 错误: SUPABASE_URL 未配置")
        print("   请在 .env 文件中设置 SUPABASE_URL")
        sys.exit(1)
    
    if not SUPABASE_KEY:
        print("❌ 错误: SUPABASE_ANON_KEY 或 SUPABASE_SERVICE_KEY 未配置")
        print("   请在 .env 文件中设置 SUPABASE_ANON_KEY 或 SUPABASE_SERVICE_KEY")
        sys.exit(1)
    
    print(f"\n📋 配置信息:")
    print(f"   SUPABASE_URL: {SUPABASE_URL}")
    print(f"   使用的 Key: {'SERVICE_KEY' if SUPABASE_SERVICE_KEY else 'ANON_KEY'}")
    print(f"   Key 前缀: {SUPABASE_KEY[:20]}...")
    
    # 创建客户端
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("\n✅ Supabase 客户端创建成功")
    except Exception as e:
        print(f"\n❌ 创建客户端失败: {e}")
        sys.exit(1)
    
    # 检查连接
    print("\n" + "-"*60)
    print("检查连接...")
    print("-"*60)
    connected, msg = check_connection(client)
    if not connected:
        print(f"❌ {msg}")
        sys.exit(1)
    print(f"✅ {msg}")
    
    # 列出可访问的表
    accessible_tables, inaccessible_tables = list_tables(client)
    
    # 显示详细信息
    if accessible_tables:
        print("\n" + "="*60)
        print("可访问的表详细信息:")
        print("="*60)
        
        for table_info in accessible_tables:
            table_name = table_info["name"]
            print(f"\n📊 表: {table_name}")
            print("-" * 60)
            
            info = get_table_info(client, table_name)
            if info.get("exists"):
                print(f"   状态: ✅ 存在")
                if info.get("columns"):
                    print(f"   列数: {len(info['columns'])}")
                    print(f"   列名: {', '.join(info['columns'][:10])}{'...' if len(info['columns']) > 10 else ''}")
                if info.get("sample_count") is not None:
                    print(f"   示例数据: {info['sample_count']} 条")
            else:
                print(f"   状态: ❌ {info.get('error', '未知错误')}")
    
    # 总结
    print("\n" + "="*60)
    print("总结:")
    print("="*60)
    print(f"✅ 可访问的表: {len(accessible_tables)}")
    print(f"❌ 不可访问的表: {len(inaccessible_tables)}")
    
    if inaccessible_tables:
        print("\n不可访问的表详情:")
        for table_info in inaccessible_tables:
            print(f"   - {table_info['name']}: {table_info['reason']}")
    
    # 建议
    print("\n" + "="*60)
    print("建议:")
    print("="*60)
    
    if "jobs" not in [t["name"] for t in accessible_tables]:
        print("⚠️  'jobs' 表不存在或不可访问")
        print("   请执行 supabase_schema.sql 创建表")
    
    if "video_tasks" not in [t["name"] for t in accessible_tables]:
        print("⚠️  'video_tasks' 表不存在或不可访问")
        print("   请执行 apps/agent/supabase_queue_schema.sql 创建表")
    
    if not SUPABASE_SERVICE_KEY and SUPABASE_ANON_KEY:
        print("ℹ️  当前使用 ANON_KEY，某些操作可能受 RLS 策略限制")
        print("   建议后端使用 SUPABASE_SERVICE_KEY 以获得完整权限")
    
    print("\n✅ 检查完成")

if __name__ == "__main__":
    main()

