# config.py
# ============================================
# ⚙️ КОНФИГУРАЦИЯ ПОДКЛЮЧЕНИЯ К СЕРВЕРУ
# ============================================
import os
from dotenv import load_dotenv
import logging


# Загружаем переменные из .env файла
load_dotenv()

# Получаем переменные окружения
server_ip = os.environ.get("SERVER_IP")


# ============================================
# 📋 НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler("spectrum.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SpectrumAssistant")

# ============================================
# 🌐 НАСТРОЙКИ СЕРВЕРА
# ============================================
SERVER_IP = server_ip         # IP сервера (можно изменить)
ASK_URL = f"http://{SERVER_IP}:8080/ask"
WS_URL = f"ws://{SERVER_IP}:8080/ws"

print(f"Ваш сервер: {server_ip}")