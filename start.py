#!/usr/bin/env python3
"""启动 Shell Agent Web 服务"""
import asyncio
import uvicorn
from pathlib import Path
import sys

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from shell_agent.web.app import create_app

def main():
    config_path = "config/agent.yaml"
    host = "127.0.0.1"
    port = 8010
    
    print(f"正在启动 Shell Agent Web 服务: http://{host}:{port}")
    print(f"加载配置: {config_path}")
    
    web_app = create_app(config_path=config_path)
    
    uvicorn.run(
        web_app,
        host=host,
        port=port,
        log_level="info",
    )

if __name__ == "__main__":
    main()
