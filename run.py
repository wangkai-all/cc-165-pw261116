#!/usr/bin/env python3
"""warcview 启动入口: python3 run.py [--host 127.0.0.1] [--port 8000] [--data ./data]"""
from warcview.server import main

if __name__ == "__main__":
    main()
