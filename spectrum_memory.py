# spectrum_memory.py
import json
import os
import datetime

MEMORY_FILE = "spectrum_memory.json"
MAX_HISTORY = 20

class SpectrumMemory:
    def __init__(self):
        self.history = []
        self.settings = {
            "color": [90, 95, 130],
            "voice_rate": "+0%",
            "voice_volume": "+0%",
            "eco_mode": False
        }
        self.device_id = None
        self.token = None
        self.load()
    
    def load(self):
        try:
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.history = data.get("history", [])
                    self.settings = data.get("settings", self.settings)
                    self.device_id = data.get("device_id")
                    self.token = data.get("token")
        except:
            pass
    
    def save(self):
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "history": self.history[-MAX_HISTORY:],
                    "settings": self.settings,
                    "device_id": self.device_id,
                    "token": self.token
                }, f, ensure_ascii=False, indent=2)
        except:
            pass
    
    def add_dialog(self, user_text, bot_text):
        self.history.append({
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "user": user_text,
            "bot": bot_text
        })
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.save()
    
    def update_settings(self, **kwargs):
        for key, value in kwargs.items():
            if key in self.settings:
                self.settings[key] = value
        self.save()
    
    def get_context(self, count=3):
        """Для отправки на сервер"""
        context = []
        for d in self.history[-count:]:
            context.append(f"Пользователь: {d['user']}")
            context.append(f"Спектр: {d['bot']}")
        return "\n".join(context)

memory = SpectrumMemory()