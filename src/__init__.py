"""
CAD MCP 서비스 패키지
"""

import os
import json
import logging

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger('cad_mcp')

# 설정 로드
def load_config():
    """설정 파일을 로드한다"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info("설정 파일 로드 성공")
        return config
    except Exception as e:
        logger.error(f"설정 파일 로드 실패: {str(e)}")
        # 기본 설정 반환
        return {
            "server": {
                "name": "CAD MCP Server",
                "version": "1.0.0",
                "host": "0.0.0.0",
                "port": 5000,
                "debug": True
            },
            "cad": {
                "type": "AUTOCAD",
                "startup_wait_time": 20,
                "command_delay": 0.5
            },
            "output": {
                "directory": "output",
                "default_filename": "cad_drawing.dwg"
            }
        }

# 설정 내보내기
config = load_config()

__all__ = [
    'config'
]
