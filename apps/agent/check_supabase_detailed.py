#!/usr/bin/env python3
"""
详细检查 Supabase 数据库表结构和数据

使用方法：
    cd apps/agent
    python3 check_supabase_detailed.py
"""

import os
import json
from dotenv import load_dotenv
from supabase import create_client

# 加载 .env 文件
load_dotenv()

# 获取环境变量
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# 优先使用 SERVICE_KEY，如果没有则使用 ANON_KEY
SUPABASE_KEY = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY

def get_table_schema(client, table_name: str):
    """获取表的详细结构"""
    try:
        # 获取示例数据以推断结构
        result = client.table(table_name).select("*").limit(1).execute()
        
        if result.data and len(result.data) > 0:
            sample = result.data[0]
            schema = {}
            for key, value in sample.items():
                schema[key] = {
                    "type": type(value).__name__,
                    "sample_value": str(value)[:50] if value is not None else None
                }
            return schema
        else:
            # 如果没有数据，尝试查询表结构（通过限制查询）
            # 注意：Supabase REST API 无法直接获取表结构，只能通过数据推断
            return None
    except Exception as e:
        return {"error": str(e)}

def get_table_stats(client, table_name: str):
    """获取表的统计信息"""
    try:
        # 获取总记录数（通过查询所有记录，但只返回计数）
        # 注意：Supabase REST API 不直接支持 COUNT，我们需要查询所有记录
        result = client.table(table_name).select("*", count="exact").limit(1).execute()
        
        # 尝试获取更多数据以了解表的内容
        sample = client.table(table_name).select("*").limit(10).execute()
        
        return {
            "count": result.count if hasattr(result, 'count') else len(sample.data) if sample.data else 0,
            "sample_rows": len(sample.data) if sample.data else 0
        }
    except Exception as e:
        return {"error": str(e)}

def main():
    print("="*70)
    print("Supabase 数据库详细检查")
    print("="*70)
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ 环境变量未配置")
        return
    
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 检查的表
    tables_to_check = ["jobs", "prompts_library", "profiles", "video_tasks"]
    
    for table_name in tables_to_check:
        print("\n" + "="*70)
        print(f"📊 表: {table_name}")
        print("="*70)
        
        try:
            # 检查表是否存在
            result = client.table(table_name).select("*").limit(1).execute()
            
            print(f"✅ 状态: 存在且可访问")
            
            # 获取表结构
            schema = get_table_schema(client, table_name)
            if schema and "error" not in schema:
                print(f"\n📋 表结构 ({len(schema)} 列):")
                for col_name, col_info in schema.items():
                    sample = col_info.get("sample_value", "N/A")
                    if sample and len(sample) > 40:
                        sample = sample[:40] + "..."
                    print(f"   - {col_name:20s} ({col_info['type']:10s}) 示例: {sample}")
            
            # 获取统计信息
            stats = get_table_stats(client, table_name)
            if "error" not in stats:
                print(f"\n📈 统计信息:")
                print(f"   - 示例数据行数: {stats.get('sample_rows', 0)}")
            
            # 显示示例数据
            sample_data = client.table(table_name).select("*").limit(3).execute()
            if sample_data.data and len(sample_data.data) > 0:
                print(f"\n📄 示例数据 (前 {len(sample_data.data)} 条):")
                for i, row in enumerate(sample_data.data, 1):
                    print(f"\n   记录 {i}:")
                    for key, value in row.items():
                        if value is None:
                            print(f"      {key}: null")
                        elif isinstance(value, (dict, list)):
                            print(f"      {key}: {json.dumps(value, ensure_ascii=False)[:100]}...")
                        else:
                            val_str = str(value)
                            if len(val_str) > 60:
                                val_str = val_str[:60] + "..."
                            print(f"      {key}: {val_str}")
            
        except Exception as e:
            error_msg = str(e)
            if "relation" in error_msg.lower() or "does not exist" in error_msg.lower():
                print(f"❌ 状态: 表不存在")
            elif "permission" in error_msg.lower() or "policy" in error_msg.lower():
                print(f"⚠️  状态: 权限不足（RLS 策略限制）")
            else:
                print(f"❌ 错误: {error_msg[:200]}")
    
    print("\n" + "="*70)
    print("检查完成")
    print("="*70)
    
    # 显示配置信息
    print(f"\n📋 当前配置:")
    print(f"   SUPABASE_URL: {SUPABASE_URL}")
    print(f"   使用的 Key: {'SERVICE_KEY' if SUPABASE_SERVICE_KEY else 'ANON_KEY'}")
    
    if not SUPABASE_SERVICE_KEY:
        print(f"\n⚠️  提示: 当前使用 ANON_KEY，某些操作可能受 RLS 策略限制")
        print(f"   建议后端使用 SUPABASE_SERVICE_KEY 以获得完整权限")

if __name__ == "__main__":
    main()

