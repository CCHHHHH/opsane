#!/usr/bin/env python3
"""Shell Agent 服务启动"""
import sys
from pathlib import Path
import argparse

# 确保项目根目录在 sys.path 中
root_dir = Path(__file__).parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import uvicorn
from shell_agent.web.app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shell Agent Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8010, help="监听端口")
    parser.add_argument("--config", default="config/agent.yaml", help="配置文件路径")
    args = parser.parse_args()
    
    print("=" * 60)
    print("  Shell Agent Web 服务")
    print("=" * 60)
    
    # 创建应用
    app = create_app(config_path=args.config)
    
    # 启动服务器
    print(f"\n服务启动: http://{args.host}:{args.port}")
    print(f"在浏览器打开上述地址使用\n")
    
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=True,
    )
