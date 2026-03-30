import pygame
import random
import math
import requests
import threading
import speech_recognition as sr
import edge_tts
import asyncio
import time
import json
import websocket
import datetime
import webbrowser
import os
import logging
import io
from queue import Queue
from threading import Timer
import re
from spectrum_memory import memory
from spectrum_contacts import ContactsManager
from call_manager import CallManager
import sys


from config import (
    logger, SERVER_IP, ASK_URL, WS_URL,
)


def exception_handler(exc_type, exc_value, exc_traceback):
    logger.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = exception_handler

def thread_exception_handler(args):
    logger.error(f"Необработанное исключение в потоке {args.thread.name}: {args.exc_value}")
    sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)

def thread_exception_handler(args):
    logger.error(f"Необработанное исключение в потоке {args.thread.name}: {args.exc_value}")
    import traceback
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    # Не завершаем программу
threading.excepthook = thread_exception_handler

# ============================================
# 🎵 ПОТОКОВОЕ АУДИО (PyAudio + pydub)
# ============================================
import pyaudio
from io import BytesIO
from pydub import AudioSegment
# Добавьте эти 4 строки:
from static_ffmpeg import add_paths
add_paths()


# Глобальные переменные для PyAudio
_pyaudio_instance = None
_audio_stream = None

def _init_pyaudio():
    """Инициализирует PyAudio один раз при первом вызове."""
    global _pyaudio_instance, _audio_stream
    if _pyaudio_instance is None:
        _pyaudio_instance = pyaudio.PyAudio()
        _audio_stream = _pyaudio_instance.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=24000,
            output=True,
            frames_per_buffer=4096
        )
        logging.getLogger("SpectrumAssistant").debug("🎵 PyAudio инициализирован для потокового воспроизведения")

def _close_pyaudio():
    """Закрывает PyAudio поток (вызывается при завершении программы)."""
    global _pyaudio_instance, _audio_stream
    if _audio_stream is not None:
        try:
            _audio_stream.stop_stream()
            _audio_stream.close()
        except Exception as e:
            logger.error(f"Ошибка закрытия аудиопотока: {e}")
        _audio_stream = None
    if _pyaudio_instance is not None:
        try:
            _pyaudio_instance.terminate()
        except Exception as e:
            logger.error(f"Ошибка завершения PyAudio: {e}")
        _pyaudio_instance = None
        logging.getLogger("SpectrumAssistant").debug("🎵 PyAudio закрыт")

# ============================================
# 📨 РАБОТА С СООБЩЕНИЯМИ И АНИМАЦИЯМИ
# ============================================

MY_DEVICE_ID = None          # будет установлено после регистрации
MY_TOKEN = None              # токен устройства

def register_device():
    """Регистрирует устройство на сервере, сохраняет device_id и token"""
    global MY_DEVICE_ID, MY_TOKEN
    # Если уже есть сохранённые данные, используем их
    if memory.device_id and memory.token:
        MY_DEVICE_ID = memory.device_id
        MY_TOKEN = memory.token
        logger.info(f"🔑 Используем сохранённые данные: device_id={MY_DEVICE_ID}")
        # Проверяем валидность токена (можно отправить ping, но пока считаем валидным)
        return
    # Регистрируемся на сервере
    try:
        logger.info("🔑 Регистрация устройства на сервере...")
        response = requests.post(f"http://{SERVER_IP}:8080/register", json={"device_id": None}, timeout=10)
        if response.status_code == 200:
            data = response.json()
            MY_DEVICE_ID = data["device_id"]
            MY_TOKEN = data["token"]
            # Сохраняем в память
            memory.device_id = MY_DEVICE_ID
            memory.token = MY_TOKEN
            memory.save()
            logger.info(f"✅ Устройство зарегистрировано: device_id={MY_DEVICE_ID}")
        else:
            logger.error(f"❌ Ошибка регистрации: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка регистрации: {e}")

def send_message(recipient_id, text):
    try:
        logger.info(f"📤 Отправка сообщения для {recipient_id}: {text[:50]}...")
        response = requests.post(f"http://{SERVER_IP}:8080/send_message", json={
            "sender_id": MY_DEVICE_ID,
            "recipient_id": recipient_id,
            "text": text,
            "token": MY_TOKEN               # <-- передаём токен
        }, timeout=10)
        if response.status_code == 200:
            logger.info(f"✅ Сообщение отправлено {recipient_id}")
            return True
        elif response.status_code == 401:
            logger.error("❌ Ошибка аутентификации, пробуем перерегистрацию")
            register_device()
            return False
        else:
            logger.error(f"❌ Ошибка отправки: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения: {e}")
        return False

def fetch_new_messages():
    try:
        logger.info("📩 Запрос непрочитанных сообщений...")
        response = requests.get(f"http://{SERVER_IP}:8080/get_messages", 
                                params={"recipient_id": MY_DEVICE_ID, "token": MY_TOKEN}, timeout=10)
        if response.status_code == 200:
            msgs = response.json().get("messages", [])
            if msgs:
                logger.info(f"📨 Получено {len(msgs)} непрочитанных сообщений")
                incoming_messages.extend(msgs)
                notify_new_message()
            else:
                logger.info("📭 Нет новых сообщений")
        elif response.status_code == 401:
            logger.error("❌ Ошибка аутентификации, пробуем перерегистрацию")
            register_device()
    except Exception as e:
        logger.error(f"❌ Ошибка получения сообщений: {e}")

def notify_new_message():
    global waiting_for_message_read, last_incoming_message
    if incoming_messages:
        last_incoming_message = incoming_messages[-1]
        waiting_for_message_read = True
        logger.info(f"🔔 Уведомление о новом сообщении от {last_incoming_message['sender_id']}")
        speak_local(f"Получено сообщение от {last_incoming_message['sender_id']}. Прочитать сейчас?", save_user_text="")

def notify_incoming_call(from_id, call_id):
    global waiting_for_call_answer, pending_call_request
    waiting_for_call_answer = True
    pending_call_request = (from_id, call_id)
    logger.info(f"📞 Входящий звонок от {from_id}")
    speak_local(f"Входящий звонок от {from_id}. Ответить?", save_user_text="")

# ============================================
# 📞 ФУНКЦИИ ДЛЯ ЗВОНКОВ (отправка через WebSocket)
# ============================================
def send_call_request(target_id):
    global main_ws
    if main_ws:
        msg = json.dumps({
            "type": "call_request",
            "target_id": target_id,
            "from_id": MY_DEVICE_ID,
            "token": MY_TOKEN
        })
        main_ws.send(msg)
        logger.info(f"📞 Отправлен запрос звонка для {target_id}")

def send_call_answer(call_id, accept):
    global main_ws
    if main_ws:
        msg = json.dumps({
            "type": "call_answer",
            "call_id": call_id,
            "accept": accept,
            "device_id": MY_DEVICE_ID,
            "token": MY_TOKEN
        })
        main_ws.send(msg)
        logger.info(f"📞 Отправлен ответ на звонок {call_id}: accept={accept}")

def send_hangup(call_id):
    global main_ws
    if not call_id:   # <-- добавь эту строку
        logger.warning("Попытка отправить отбой без call_id")
        return
    if main_ws:
        msg = json.dumps({
            "type": "call_end",
            "call_id": call_id,
            "device_id": MY_DEVICE_ID
        })
        main_ws.send(msg)
        logger.info(f"📞 Отправлен отбой звонка {call_id}")

# ============================================
# 🎬 АНИМАЦИИ
# ============================================
def start_collapse_animation(center=(300,300)):
    global collapse_animation, collapse_center, collapse_timer, collapse_particles, active_p_count, particles
    collapse_animation = True
    collapse_center = center
    collapse_timer = 0.5
    collapse_particles = [p for i, p in enumerate(particles) if i < active_p_count]
    logger.debug("💥 Запущена анимация коллапса")

def start_shake_animation(duration=0.3, intensity=5):
    global shake_animation, shake_timer, shake_intensity, red_override
    shake_animation = True
    shake_timer = duration
    shake_intensity = intensity
    red_override = (255, 0, 0)
    logger.debug("🔄 Запущена анимация дрожи")

def start_send_success_animation():
    global send_animation, send_timer, send_particles, send_velocities, active_p_count, particles
    send_animation = True
    send_timer = 0.8
    send_particles = [p for i, p in enumerate(particles) if i < active_p_count]
    send_velocities = [(random.uniform(-1, 1), random.uniform(-15, -5)) for _ in send_particles]
    logger.debug("✈️ Запущена анимация отправки")

# Переменные для анимаций
collapse_animation = False
collapse_center = (300, 300)
collapse_timer = 0.0
collapse_particles = []

shake_animation = False
shake_timer = 0.0
shake_intensity = 5
red_override = None

send_animation = False
send_timer = 0.0
send_particles = []
send_velocities = []

# Состояния диалога отправки сообщения
waiting_for_message_contact = False
waiting_for_message_text = False
waiting_for_message_confirm = False
temp_message_contact = None
temp_message_text = ""

# Ожидание чтения входящего сообщения
waiting_for_message_read = False
last_incoming_message = None
incoming_messages = []

# Новые переменные для ответа на сообщение
waiting_for_message_reply = False
temp_reply_sender = None

# Флаг отмены ответа после стоп-команды
stop_requested = False

# ============================================
# 🎯 ГЛОБАЛЬНЫЕ СПИСКИ КОМАНД
# ============================================
CALL_PHRASES = [
    "позвоним", "позвонить", "набери", "звонок", "позвони", "набирай", "наберем",
    "звякни", "звякнуть", "созвон", "вызывай", "вызов", "набери-ка", "свяжись",
    "соедини", "дозвонись", "звони", "набирай номер", "трубку", "набери мне",
    "соверши звонок", "позвони человеку", "набери контакт", "свяжись с абонентом",
    "позвони по номеру", "звонок абоненту", "набери телефон", "совершить вызов"
]

MESSAGE_PHRASES = [
    "напишем", "написать", "отправь", "сообщение", "напиши", "черкани", "черкни",
    "смс", "смску", "sms", "письмецо", "сообщеньку", "отправь текст", "скинь инфу",
    "напечатай", "месседж", "мессагу", "сообщи", "текстани", "накатай", "запули",
    "отгрузи текст", "в личку", "личку", "скинь сообщение", "отправь смс", "напиши смс",
    "отправить сообщение", "послать сообщение", "отправка смс", "текстовое сообщение",
    "отправь сообщение", "напиши сообщение"
]

OPEN_CONTACTS_PHRASES = [
    "контакты", "справочник", "мои контакты", "покажи контакты", "открой контакты",
    "открыть контакты", "покажи справочник", "открой справочник"
]

CLOSE_CONTACTS_PHRASES = [
    "закрой контакты", "выйти из контактов", "закрыть контакты", "скрой контакты",
    "выйти из справочника", "закрыть справочник", "скрой справочник", "выход из контактов"
]

# Глобальный список имён для пробуждения (включая английские варианты)
WAKE_WORDS = [
    "спектр", "спектра", "спектру", "спектре", "спектры", "спе", "спек",
    "spectrum", "spectr", "spector", "spctr", "spectra"
]


is_thinking = False
is_recording = False
is_speaking = False
is_envelope = False
is_calling = False
is_contacts = False
is_active_session = False
current_emotion = "neutral"
last_command_time = 0
SESSION_TIMEOUT = 45                # Таймаут неактивной сессии (сек)
global_recognizer = None
# Для звонков
call_manager = None
waiting_for_call_answer = False
pending_call_request = None   # будет хранить (from_id, call_id)
main_ws = None                # сюда сохраним основной WebSocket
waiting_for_call_name = False   # для режима "Кому позвонить?"
current_call_id = None          # ID активного звонка
custom_particle_count = None   # если None, используется PARTICLE_COUNT или eco_mode значение

# ============================================
# 👥 КОНТАКТЫ
# ============================================
contacts_manager = ContactsManager()
contact_shapes = []
contacts_ring_radius = 155
contacts_ring_center = (300, 300)

# Состояния диалога добавления контакта
waiting_for_contact_name = False
waiting_for_contact_id = False
waiting_for_contact_confirm = False
temp_contact_name = ""
temp_contact_id = ""

# ID контакта для звонка/сообщения
call_target_id = None
message_target_id = None

# Состояния диалога удаления контакта
waiting_for_contact_delete = False

# ============================================
# 🗣️ ФРАЗЫ ДЛЯ ДИАЛОГА
# ============================================
CALL_QUESTIONS = [
    "Кому позвонить?",
    "Какой номер набрать?",
    "Кому звоним?",
    "Скажите имя или номер",
    "Кому совершить звонок?"
]

MESSAGE_QUESTIONS = [
    "Кому отправить сообщение?",
    "Какой контакт?",
    "Кому написать?",
    "Укажите получателя",
    "Кому адресовать сообщение?"
]

WAKE_ANSWERS = [
    "Слушаю",
    "Да?",
    "Я здесь",
    "Чем могу помочь?",
    "Слушаю вас"
]

is_loading = False
is_warning = False
is_processing = False

last_shape_state = None

eco_mode = False
PARTICLE_COUNT = 1600               # Количество частиц (можно уменьшить для экономии)

cloud_center_x, cloud_center_y = 300, 300
cloud_target_x, cloud_target_y = 300, 300

base_color = [90, 95, 130]
global_r, global_g, global_b = 90.0, 95.0, 130.0

# ============================================
# 🎛️ НАСТРОЙКИ ГОЛОСА
# ============================================
VOICE_RATE = "+0%"                  # Диапазон -50% … +50%
VOICE_VOLUME = "+0%"                # Диапазон -100% … +100%
VOICE_NAME = "ru-RU-SvetlanaNeural" # Имя голоса edge-tts

base_color = memory.settings["color"]
VOICE_RATE = memory.settings["voice_rate"]
VOICE_VOLUME = memory.settings["voice_volume"]
eco_mode = memory.settings["eco_mode"]
logger.info(f"⚙️ Загружены настройки: цвет={base_color}, скорость={VOICE_RATE}, громкость={VOICE_VOLUME}")

# ============================================
# ⏰ ТАЙМЕРЫ И НАПОМИНАНИЯ
# ============================================
timers = []
reminders = []

class Reminder:
    def __init__(self, text, target_time):
        self.text = text
        self.target_time = target_time
        self.triggered = False

def parse_time(time_str):
    """Парсит время из строки: 'через 5 минут', 'через 2 часа', 'в 15:30'"""
    time_str = time_str.lower().strip()
    match = re.match(r'через\s+(\d+)\s*(минут|секунд|часов|мин|сек|ч)', time_str)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        if unit in ['минут', 'мин']:
            return time.time() + num * 60
        elif unit in ['секунд', 'сек']:
            return time.time() + num
        elif unit in ['часов', 'ч']:
            return time.time() + num * 3600
    match = re.match(r'в\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3)) if match.group(3) else 0
        now = datetime.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target < now:
            target += datetime.timedelta(days=1)
        return target.timestamp()
    return None

def check_timers():
    global timers, reminders
    current_time = time.time()
    for timer in timers[:]:
        if timer[0] <= current_time and not timer[2]:
            timer[2] = True
            speak_local(f"Время вышло! {timer[1]}" if timer[1] else "Время вышло!")
            show_clock_animation()
    for reminder in reminders[:]:
        if reminder.target_time <= current_time and not reminder.triggered:
            reminder.triggered = True
            speak_local(f"Напоминание: {reminder.text}")
            reminders.remove(reminder)

def show_clock_animation():
    global is_loading
    is_loading = True
    def reset_animation():
        global is_loading
        is_loading = False
    threading.Timer(2.0, reset_animation).start()

# ============================================
# 🗣️ ГОЛОСОВОЙ ДВИЖОК (ПОТОКОВАЯ ВЕРСИЯ)
# ============================================
def clean_response(text):
    """Удаляет маркдаун символы и лишние пробелы из текста для озвучивания."""
    text = re.sub(r'[*_#]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def find_silence_rms(pcm_data, sample_width=2, max_search=9600, silence_threshold=500, min_silence_len=2400):
    """
    Ищет в конце pcm_data участок тишины длительностью min_silence_len байт.
    silence_threshold - порог RMS (чем ниже, тем тише). 500 = ~ -30dB.
    max_search - сколько байт просмотреть от конца (по умолч. 9600 = 200 мс).
    min_silence_len - минимальная длина тишины в байтах (2400 = 50 мс при 24 кГц).
    Возвращает индекс начала тишины (байт), ближайший к концу.
    """
    search_end = len(pcm_data)
    search_start = max(0, search_end - max_search)
    # выравниваем на чётные
    if search_start % 2 != 0:
        search_start += 1
    if search_end % 2 != 0:
        search_end -= 1
    if search_end - search_start < min_silence_len:
        return search_end

    # Идём с конца и ищем блок тишины
    best_start = search_end
    silence_start = None
    silence_len = 0
    for i in range(search_end, search_start, -2):
        # вычисляем RMS на 20 мс (960 байт) - можно меньше
        frame_end = i
        frame_start = max(search_start, i - 960)
        if frame_end - frame_start < 32:
            continue
        # RMS
        sum_sq = 0
        for j in range(frame_start, frame_end, 2):
            val = int.from_bytes(pcm_data[j:j+2], 'little', signed=True)
            sum_sq += val * val
        rms = (sum_sq / ((frame_end - frame_start)//2)) ** 0.5
        if rms < silence_threshold:
            if silence_start is None:
                silence_start = i
            silence_len += (frame_end - frame_start)
        else:
            if silence_start is not None and silence_len >= min_silence_len:
                best_start = silence_start - silence_len
                break
            silence_start = None
            silence_len = 0
    # если нашли тишину в конце
    if silence_start is not None and silence_len >= min_silence_len:
        best_start = silence_start - silence_len
    # выравниваем
    if best_start % 2 != 0:
        best_start -= 1
    return best_start

def speak_full(text, save_user_text=""):
    global is_speaking, current_emotion, stop_requested, last_command_time

    if stop_requested:
        logger.info("🔇 Речь отменена по стоп-команде до начала генерации")
        stop_requested = False
        return

    text = clean_response(text)
    if not text:
        return

    logger.info(f"🔊 Голос: '{text[:80]}...' | Эмоция: {detect_emotion(text)} | Скорость: {VOICE_RATE} | Громкость: {VOICE_VOLUME}")
    current_emotion = detect_emotion(text)
    if save_user_text:
        memory.add_dialog(save_user_text, text)

    def run():
        global is_speaking, current_emotion, stop_requested
        last_command_time = time.time()

        _init_pyaudio()
        
        mp3_buffer = bytearray()
        MIN_MP3_BUFFER_SIZE = 49152           # 24 КБ – декодируем большими блоками
        pcm_buffer = bytearray()
        PCM_CHUNK_SIZE = 115200                # 600 мс – редко отправляем в PyAudio

        try:
            communicate = edge_tts.Communicate(
                text,
                VOICE_NAME,
                rate=VOICE_RATE,
                volume=VOICE_VOLUME
            )

            is_speaking = True

            async def _stream_audio():
                nonlocal mp3_buffer, pcm_buffer
                async for chunk in communicate.stream():
                    if stop_requested:
                        logger.info("🔇 Речь прервана стоп-командой")
                        break
                    if chunk["type"] == "audio":
                        mp3_buffer.extend(chunk["data"])
                        if len(mp3_buffer) >= MIN_MP3_BUFFER_SIZE:
                            try:
                                audio_segment = AudioSegment.from_mp3(BytesIO(mp3_buffer))
                                new_pcm = audio_segment.raw_data
                                mp3_buffer.clear()
                                
                                # Плавное добавление с перекрытием (crossfade)
                                if pcm_buffer:
                                    cross_len = 4800   # 100 мс при 24 кГц (4800 байт)
                                    if len(pcm_buffer) >= cross_len and len(new_pcm) >= cross_len:
                                        # Берём хвост старого буфера и начало нового
                                        tail = pcm_buffer[-cross_len:]
                                        head = new_pcm[:cross_len]
                                        mixed = bytearray(cross_len)
                                        # Линейное смешивание: старый затухает, новый нарастает
                                        for i in range(0, cross_len, 2):
                                            old_val = int.from_bytes(tail[i:i+2], 'little', signed=True)
                                            new_val = int.from_bytes(head[i:i+2], 'little', signed=True)
                                            factor = i / cross_len   # от 0 до 1 для нового блока
                                            mixed_val = int(old_val * (1 - factor) + new_val * factor)
                                            mixed[i:i+2] = mixed_val.to_bytes(2, 'little', signed=True)
                                        # Заменяем хвост старого буфера смешанным куском
                                        pcm_buffer = pcm_buffer[:-cross_len] + mixed
                                        # Добавляем остаток нового PCM после перекрытия
                                        pcm_buffer.extend(new_pcm[cross_len:])
                                    else:
                                        pcm_buffer.extend(new_pcm)
                                else:
                                    pcm_buffer.extend(new_pcm)
                                
                                while len(pcm_buffer) >= PCM_CHUNK_SIZE:
                                    if _audio_stream is not None:
                                        _audio_stream.write(bytes(pcm_buffer[:PCM_CHUNK_SIZE]))
                                    pcm_buffer = pcm_buffer[PCM_CHUNK_SIZE:]
                            except Exception as e:
                                logger.warning(f"⚠️ Ошибка декодирования: {e}")
                                mp3_buffer.clear()
                                continue
                
                if mp3_buffer and not stop_requested:
                    try:
                        audio_segment = AudioSegment.from_mp3(BytesIO(mp3_buffer))
                        pcm_buffer.extend(audio_segment.raw_data)
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка декодирования последнего блока: {e}")
                
                if pcm_buffer and _audio_stream is not None and not stop_requested:
                    _audio_stream.write(bytes(pcm_buffer))
                    pcm_buffer.clear()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_stream_audio())
            loop.close()

        except Exception as e:
            logger.error(f"❌ Ошибка потоковой речи: {e}")
        finally:
            is_speaking = False
            last_command_time = time.time()
            current_emotion = "neutral"
            logger.info("🔇 Речь завершена")

    threading.Thread(target=run, daemon=True).start()

speak_local = speak_full

def stop_speaking():
    global is_speaking, stop_requested
    stop_requested = True
    is_speaking = False
    try:
        pygame.mixer.music.stop()
        pygame.mixer.music.unload()
        time.sleep(0.05)
        logger.info("🔇 Принудительная остановка речи")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

# ============================================
# 😊 ЭМОЦИОНАЛЬНЫЙ ДВИЖОК
# ============================================
def detect_emotion(text):
    text = text.lower()
    emotions = {
        "happy": ["круто", "отлично", "рад", "ха-ха", "привет", "супер", "кайф", "спасибо"],
        "sad": ["грустно", "жаль", "прости", "нет", "плохо"],
        "surprised": ["что", "как", "ого", "вау", "серьезно"],
        "angry": ["бесит", "плохо", "хватит", "ужасно", "злой"]
    }
    for emotion, keywords in emotions.items():
        if any(word in text for word in keywords): return emotion
    return "neutral"

# ========== НОВАЯ УЛУЧШЕННАЯ ФУНКЦИЯ НОРМАЛИЗАЦИИ И ПОИСКА ==========
def normalize_name(name):
    """
    Возвращает основу имени (удаляет типичные падежные окончания).
    Не добавляет 'а', чтобы не испортить имена типа "артём".
    """
    name = name.strip().lower()
    if not name:
        return ""
    endings = ['е', 'у', 'ю', 'ой', 'ей', 'ом', 'ём', 'ого', 'его']
    for ending in endings:
        if name.endswith(ending):
            return name[:-len(ending)].capitalize()
    return name.capitalize()

def find_contact_by_name(raw_name):
    """
    Ищет контакт по имени, пробуя разные формы:
    - прямое совпадение
    - основа (после удаления окончания)
    - основа + 'а' (для имён типа "дим" -> "дима")
    """
    if not raw_name:
        return None
    name = raw_name.strip().lower()
    # 1. Прямое совпадение
    contact = contacts_manager.get_contact_by_name(name)
    if contact:
        return contact
    # 2. Основа (убираем окончания)
    base = normalize_name(name).lower()
    if base:
        contact = contacts_manager.get_contact_by_name(base)
        if contact:
            return contact
    # 3. Основа + 'а' (для мужских имён, которые в именительном падеже оканчиваются на 'а')
    if base and base[-1] not in 'ая':
        base_a = base + 'а'
        contact = contacts_manager.get_contact_by_name(base_a)
        if contact:
            return contact
    # 4. Поиск по вхождению (как запасной вариант)
    for c in contacts_manager.contacts:
        if c.name.lower() in name or name in c.name.lower():
            return c
    return None

# ============================================
# 🖼️ ФИГУРЫ ДЛЯ КОНТАКТОВ
# ============================================
def get_contact_shape_point(shape_type, center, idx, total, rotation=0):
    x0, y0 = center
    if shape_type.startswith('shape_'):
        try:
            number = int(shape_type.split('_')[1])
            num_sides = number + 3
            angle = 2 * math.pi * idx / total + rotation
            seg_size = 2 * math.pi / num_sides
            seg_index = int(angle // seg_size)
            seg_start_angle = seg_index * seg_size
            seg_end_angle = seg_start_angle + seg_size
            r = 25
            v1_x = r * math.cos(seg_start_angle)
            v1_y = r * math.sin(seg_start_angle)
            v2_x = r * math.cos(seg_end_angle)
            v2_y = r * math.sin(seg_end_angle)
            t = (angle - seg_start_angle) / seg_size
            x = v1_x + (v2_x - v1_x) * t
            y = v1_y + (v2_y - v1_y) * t
            return (x0 + x, y0 + y)
        except:
            angle = 2 * math.pi * idx / total + rotation
            return (x0 + 25 * math.cos(angle), y0 + 25 * math.sin(angle))
    if shape_type == 'circle':
        angle = 2 * math.pi * idx / total + rotation
        return (x0 + 25 * math.cos(angle), y0 + 25 * math.sin(angle))
    elif shape_type == 'square':
        side = 35
        perimeter = 4 * side
        t = (idx / total) * perimeter
        if t < side:
            return (x0 - side/2 + t, y0 - side/2)
        elif t < 2*side:
            return (x0 + side/2, y0 - side/2 + (t - side))
        elif t < 3*side:
            return (x0 + side/2 - (t - 2*side), y0 + side/2)
        else:
            return (x0 - side/2, y0 + side/2 - (t - 3*side))
    elif shape_type == 'triangle':
        size = 35
        top = (x0, y0 - size)
        left = (x0 - size * math.cos(math.pi/6), y0 + size * math.sin(math.pi/6))
        right = (x0 + size * math.cos(math.pi/6), y0 + size * math.sin(math.pi/6))
        side_len = math.sqrt((top[0]-left[0])**2 + (top[1]-left[1])**2)
        perimeter = 3 * side_len
        t = (idx / total) * perimeter
        if t < side_len:
            ratio = t / side_len
            x = top[0] + (left[0]-top[0]) * ratio
            y = top[1] + (left[1]-top[1]) * ratio
        elif t < 2*side_len:
            ratio = (t - side_len) / side_len
            x = left[0] + (right[0]-left[0]) * ratio
            y = left[1] + (right[1]-left[1]) * ratio
        else:
            ratio = (t - 2*side_len) / side_len
            x = right[0] + (top[0]-right[0]) * ratio
            y = right[1] + (top[1]-right[1]) * ratio
        return (x, y)
    elif shape_type == 'diamond':
        w, h = 30, 30
        top = (x0, y0 - h)
        right = (x0 + w, y0)
        bottom = (x0, y0 + h)
        left = (x0 - w, y0)
        side_len = math.sqrt(w**2 + h**2)
        perimeter = 4 * side_len
        t = (idx / total) * perimeter
        if t < side_len:
            ratio = t / side_len
            x = top[0] + (right[0]-top[0]) * ratio
            y = top[1] + (right[1]-top[1]) * ratio
        elif t < 2*side_len:
            ratio = (t - side_len) / side_len
            x = right[0] + (bottom[0]-right[0]) * ratio
            y = right[1] + (bottom[1]-right[1]) * ratio
        elif t < 3*side_len:
            ratio = (t - 2*side_len) / side_len
            x = bottom[0] + (left[0]-bottom[0]) * ratio
            y = bottom[1] + (left[1]-bottom[1]) * ratio
        else:
            ratio = (t - 3*side_len) / side_len
            x = left[0] + (top[0]-left[0]) * ratio
            y = left[1] + (top[1]-left[1]) * ratio
        return (x, y)
    elif shape_type == 'star':
        angle = 2 * math.pi * idx / total + rotation
        r1 = 25
        r2 = 12
        if (idx // (total // 10)) % 2 == 0:
            r = r1
        else:
            r = r2
        return (x0 + r * math.cos(angle), y0 + r * math.sin(angle))
    else:
        angle = 2 * math.pi * idx / total
        return (x0 + 25 * math.cos(angle), y0 + 25 * math.sin(angle))

def update_contact_shapes(dt):
    for shape in contact_shapes:
        shape['angle'] += shape['speed'] * dt
        if shape['angle'] > 2 * math.pi:
            shape['angle'] -= 2 * math.pi

def add_contact_shape(contact, angle=None):
    if angle is None:
        n = len(contact_shapes) + 1
        angle = 2 * math.pi * (n - 1) / n
    color = (hash(contact.id) % 156 + 100, (hash(contact.id) >> 8) % 156 + 100, (hash(contact.id) >> 16) % 156 + 100)
    speed = 1
    contact_shapes.append({
        'id': contact.id,
        'name': contact.name,
        'shape_type': contact.shape_type,
        'color': color,
        'angle': angle,
        'speed': speed
    })
    distribute_particles_to_contacts()

def init_contact_shapes():
    global contact_shapes
    contact_shapes = []
    for contact in contacts_manager.contacts:
        add_contact_shape(contact, angle=0)
    n = len(contact_shapes)
    for idx, shape in enumerate(contact_shapes):
        shape['angle'] = 2 * math.pi * idx / n
    distribute_particles_to_contacts()

def distribute_particles_to_contacts():
    global particles, PARTICLE_COUNT
    for i in range(PARTICLE_COUNT):
        particles[i]['contact_id'] = None
        particles[i]['contact_idx'] = 0
        particles[i]['contact_total'] = 0
        particles[i]['contact_shape_type'] = None
        particles[i]['contact_center'] = None

    if not contact_shapes:
        return

    background_ratio = 0.2
    bg_count = int(PARTICLE_COUNT * background_ratio)
    remaining = PARTICLE_COUNT - bg_count
    particles_per_contact = max(30, remaining // len(contact_shapes))

    idx = bg_count
    for shape_data in contact_shapes:
        contact = contacts_manager.get_contact_by_id(shape_data['id'])
        if not contact:
            continue
        for j in range(particles_per_contact):
            if idx >= PARTICLE_COUNT:
                break
            particles[idx]['contact_id'] = contact.id
            particles[idx]['contact_idx'] = j
            particles[idx]['contact_total'] = particles_per_contact
            particles[idx]['contact_shape_type'] = shape_data['shape_type']
            particles[idx]['contact_center'] = contacts_ring_center
            idx += 1
        if idx >= PARTICLE_COUNT:
            break

# ============================================
# 🎮 РАСШИРЕННАЯ ЛОГИКА КОМАНД
# ============================================
def reset_dialog_flags():
    """Сбрасывает все флаги многошаговых диалогов и временные переменные."""
    global waiting_for_contact_name, waiting_for_contact_id, waiting_for_contact_confirm
    global waiting_for_contact_delete, temp_contact_name, temp_contact_id
    global waiting_for_message_contact, waiting_for_message_text, waiting_for_message_confirm
    global waiting_for_message_read, waiting_for_message_reply, temp_message_contact, temp_message_text, temp_reply_sender
    global call_target_id, message_target_id

    waiting_for_contact_name = False
    waiting_for_contact_id = False
    waiting_for_contact_confirm = False
    waiting_for_contact_delete = False
    temp_contact_name = ""
    temp_contact_id = ""

    waiting_for_message_contact = False
    waiting_for_message_text = False
    waiting_for_message_confirm = False
    waiting_for_message_read = False
    waiting_for_message_reply = False
    temp_message_contact = None
    temp_message_text = ""
    temp_reply_sender = None

    call_target_id = None
    message_target_id = None
    global waiting_for_call_name, waiting_for_call_answer, current_call_id
    waiting_for_call_name = False
    waiting_for_call_answer = False
    current_call_id = None

    logger.debug("🧹 Все диалоговые флаги сброшены")

def reset_shapes():
    """Сбрасывает визуальные режимы."""
    global is_calling, is_envelope, is_loading, is_warning, is_processing, is_contacts
    is_calling = is_envelope = is_loading = is_warning = is_processing = is_contacts = False
    logger.debug("🔄 Визуальные режимы сброшены")

def handle_commands(text):
    global is_envelope, is_calling, is_contacts, last_command_time, is_active_session, is_thinking
    global is_loading, is_warning, is_processing, eco_mode, base_color
    global VOICE_RATE, VOICE_VOLUME, timers, reminders
    global waiting_for_contact_name, waiting_for_contact_id, waiting_for_contact_confirm, waiting_for_contact_delete, contact_shapes
    global call_target_id, message_target_id
    global waiting_for_message_contact, waiting_for_message_text, waiting_for_message_confirm
    global temp_message_contact, temp_message_text
    global waiting_for_message_read, waiting_for_message_reply, last_incoming_message, incoming_messages, temp_reply_sender
    global custom_particle_count
    global global_r, global_g, global_b

    text = text.lower().strip()
    logger.info(f"📝 Обработка команды: '{text}' | Проверка CALL_PHRASES: {any(phrase in text for phrase in CALL_PHRASES)}")
    logger.info(f"📝 Обработка команды: '{text}' | Состояния до: is_calling={is_calling}, is_envelope={is_envelope}, is_contacts={is_contacts}")

    # Сброс интерфейса для не-команд
    call_keywords = ["позвоним", "позвонить", "набери", "звонок", "позвони", "набирай", "вызов"]
    message_keywords = ["напишем", "написать", "отправь", "сообщение", "напиши", "смс", "sms"]
    contacts_keywords = ["контакты", "справочник", "мои контакты", "добавь контакт", "добавить контакт", "удали контакт"]

    is_call_cmd = any(kw in text for kw in call_keywords)
    is_message_cmd = any(kw in text for kw in message_keywords)
    is_contacts_cmd = any(kw in text for kw in contacts_keywords)

    if not (is_call_cmd or is_message_cmd or is_contacts_cmd):
        if is_envelope or is_calling or is_contacts:
            logger.info(f"🔄 Сброс интерфейса: конверт/телефон/контакты убираем при команде '{text[:30]}'")
            is_envelope = False
            is_calling = False
            is_contacts = False

    # Закрытие контактов
    close_contacts_phrases = [
        "закрой контакты", "выйти из контактов", "закрыть контакты", "скрой контакты",
        "выйти из справочника", "закрыть справочник", "скрой справочник", "выход из контактов"
    ]
    if any(phrase in text for phrase in close_contacts_phrases) and is_contacts:
        logger.info("🚪 Закрытие режима контактов по команде")
        is_contacts = False
        global last_shape_state
        last_shape_state = None
        speak_local("Контакты закрыты", text)
        return True

    # ========== КОМАНДЫ ОСТАНОВКИ (СБРАСЫВАЮТ ВСЁ) ==========
    sleep_phrases = [
        "отстань", "хватит", "отдыхай", "спи", "усни", "отмена", "забудь", "молчи", "стоп",
        "заткнись", "замолчи", "тихо", "тишина", "перестань", "хватит болтать", "заверши",
        "выйди", "уйди", "отойди", "прекрати", "заканчивай", "хватит говорить", "помолчи"
    ]
    if any(phrase in text for phrase in sleep_phrases):
        logger.info(f"🛑 Команда остановки: '{text}'")
        stop_speaking()
        reset_dialog_flags()
    if call_manager and call_manager.is_on_call:
        call_manager.end_call()
        reset_shapes()
        is_active_session = is_thinking = False
        logger.info("💤 Режим ожидания активирован")
        return True

    # ========== КОМАНДЫ РЕГУЛИРОВКИ СКОРОСТИ ==========
    speed_match = re.search(r'(скорость|скорости)\s*(?:речи)?\s*(?:на\s*)?(\d+)\s*%', text)
    if speed_match:
        percent = int(speed_match.group(2))
        percent = max(-50, min(50, percent))
        if percent >= 0:
            VOICE_RATE = f"+{percent}%"
        else:
            VOICE_RATE = f"{percent}%"
        speak_local(f"Скорость речи установлена на {percent}%", text)
        return True

    if ("добавь" in text and "скорости" in text) or \
       ("прибавь" in text and "скорости" in text) or \
       ("увеличь" in text and "скорость" in text) or \
       ("сделай" in text and "быстрее" in text) or \
       ("поставь" in text and "быстрее" in text) or \
       "быстрее" == text or \
       "скорости" == text:
        current = int(VOICE_RATE.replace("%", ""))
        if current + 10 <= 50:
            VOICE_RATE = f"+{current + 10}%"
        else:
            VOICE_RATE = "+50%"
        speak_local(f"Хорошо, увеличиваю скорость. Теперь {VOICE_RATE}", text)
        return True

    if ("убавь" in text and "скорости" in text) or \
       ("уменьши" in text and "скорость" in text) or \
       ("сбавь" in text and "скорость" in text) or \
       ("сделай" in text and "медленнее" in text) or \
       ("поставь" in text and "медленнее" in text) or \
       "медленнее" == text or \
       "медленней" in text:
        current = int(VOICE_RATE.replace("%", ""))
        if current - 10 >= -50:
            VOICE_RATE = f"{current - 10}%"
        else:
            VOICE_RATE = "-50%"
        speak_local(f"Хорошо, уменьшаю скорость. Теперь {VOICE_RATE}", text)
        return True

    if "говори быстрее" in text or ("быстрее" in text and "говори" in text):
        current = int(VOICE_RATE.replace("%", ""))
        if current + 10 <= 50:
            VOICE_RATE = f"+{current + 10}%"
        else:
            VOICE_RATE = "+50%"
        speak_local(f"Хорошо, буду говорить быстрее. Скорость {VOICE_RATE}", text)
        return True

    if "еще быстрее" in text or "ещё быстрее" in text:
        current = int(VOICE_RATE.replace("%", ""))
        if current + 10 <= 50:
            VOICE_RATE = f"+{current + 10}%"
        else:
            VOICE_RATE = "+50%"
        speak_local(f"Ещё быстрее! Теперь скорость {VOICE_RATE}", text)
        return True

    if "говори медленнее" in text or ("медленнее" in text and "говори" in text):
        current = int(VOICE_RATE.replace("%", ""))
        if current - 10 >= -50:
            VOICE_RATE = f"{current - 10}%"
        else:
            VOICE_RATE = "-50%"
        speak_local(f"Хорошо, буду говорить медленнее. Скорость {VOICE_RATE}", text)
        return True

    if "еще медленнее" in text or "ещё медленнее" in text:
        current = int(VOICE_RATE.replace("%", ""))
        if current - 10 >= -50:
            VOICE_RATE = f"{current - 10}%"
        else:
            VOICE_RATE = "-50%"
        speak_local(f"Ещё медленнее! Теперь скорость {VOICE_RATE}", text)
        return True

    if "нормальная скорость" in text or "сбрось скорость" in text or "стандартная скорость" in text or "средняя скорость" in text:
        VOICE_RATE = "+0%"
        memory.update_settings(voice_rate=VOICE_RATE)
        speak_local("Скорость речи сброшена до нормальной", text)
        return True

    # ========== КОМАНДЫ РЕГУЛИРОВКИ ГРОМКОСТИ ==========
    volume_match = re.search(r'(громкость|громкости)\s*(?:на\s*)?(\d+)\s*%', text)
    if volume_match:
        percent = int(volume_match.group(2))
        percent = max(-100, min(100, percent))
        if percent >= 0:
            VOICE_VOLUME = f"+{percent}%"
        else:
            VOICE_VOLUME = f"{percent}%"
        speak_local(f"Громкость установлена на {percent}%", text)
        return True

    if ("добавь" in text and "громкости" in text) or \
       ("прибавь" in text and "громкости" in text) or \
       ("увеличь" in text and "громкость" in text) or \
       ("сделай" in text and "громче" in text) or \
       ("поставь" in text and "громче" in text) or \
       "громче" == text or \
       "громкости" == text:
        current = int(VOICE_VOLUME.replace("%", ""))
        if current + 10 <= 100:
            VOICE_VOLUME = f"+{current + 10}%"
        else:
            VOICE_VOLUME = "+100%"
        speak_local(f"Хорошо, увеличиваю громкость. Теперь {VOICE_VOLUME}", text)
        return True

    if ("убавь" in text and "громкости" in text) or \
       ("уменьши" in text and "громкость" in text) or \
       ("сбавь" in text and "громкость" in text) or \
       ("сделай" in text and "тише" in text) or \
       ("поставь" in text and "тише" in text) or \
       "тише" == text or \
       "тиши" in text:
        current = int(VOICE_VOLUME.replace("%", ""))
        if current - 10 >= -100:
            VOICE_VOLUME = f"{current - 10}%"
        else:
            VOICE_VOLUME = "-100%"
        speak_local(f"Хорошо, уменьшаю громкость. Теперь {VOICE_VOLUME}", text)
        return True

    if "говори громче" in text or ("громче" in text and "говори" in text):
        current = int(VOICE_VOLUME.replace("%", ""))
        if current + 10 <= 100:
            VOICE_VOLUME = f"+{current + 10}%"
        else:
            VOICE_VOLUME = "+100%"
        speak_local(f"Хорошо, буду говорить громче. Громкость {VOICE_VOLUME}", text)
        return True

    if "еще громче" in text or "ещё громче" in text:
        current = int(VOICE_VOLUME.replace("%", ""))
        if current + 10 <= 100:
            VOICE_VOLUME = f"+{current + 10}%"
        else:
            VOICE_VOLUME = "+100%"
        speak_local(f"Ещё громче! Теперь громкость {VOICE_VOLUME}", text)
        return True

    if "говори тише" in text or ("тише" in text and "говори" in text):
        current = int(VOICE_VOLUME.replace("%", ""))
        if current - 10 >= -100:
            VOICE_VOLUME = f"{current - 10}%"
        else:
            VOICE_VOLUME = "-100%"
        speak_local(f"Хорошо, буду говорить тише. Громкость {VOICE_VOLUME}", text)
        return True

    if "еще тише" in text or "ещё тише" in text:
        current = int(VOICE_VOLUME.replace("%", ""))
        if current - 10 >= -100:
            VOICE_VOLUME = f"{current - 10}%"
        else:
            VOICE_VOLUME = "-100%"
        speak_local(f"Ещё тише! Теперь громкость {VOICE_VOLUME}", text)
        return True

    if "нормальная громкость" in text or "сбрось громкость" in text or "стандартная громкость" in text or "средняя громкость" in text:
        VOICE_VOLUME = "+0%"
        memory.update_settings(voice_volume=VOICE_VOLUME)
        speak_local("Громкость сброшена до нормальной", text)
        return True

    # ========== КОМАНДЫ ТАЙМЕРА ==========
    if "таймер" in text or "засеки время" in text:
        time_match = re.search(r'(\d+)\s*(минут|секунд|часов|мин|сек|ч)', text)
        if time_match:
            num = int(time_match.group(1))
            unit = time_match.group(2)
            if unit in ['минут', 'мин']:
                seconds = num * 60
                unit_text = f"{num} минут"
            elif unit in ['секунд', 'сек']:
                seconds = num
                unit_text = f"{num} секунд"
            else:
                seconds = num * 3600
                unit_text = f"{num} часов"
            timer_time = time.time() + seconds
            timers.append([timer_time, f"Таймер на {unit_text} истек", False])
            speak_local(f"Таймер на {unit_text} установлен", text)
            return True
        else:
            speak_local("Скажи, например, 'таймер на 5 минут' или 'засеки 30 секунд'", text)
            return True

    # ========== КОМАНДЫ НАПОМИНАНИЙ ==========
    if "напомни" in text:
        clean_text = text
        for name in WAKE_WORDS:
            clean_text = clean_text.replace(name, "")
        clean_text = clean_text.replace("эй", "").strip()
        time_match = re.search(r'через\s+(\d+)\s*(минут|секунд|часов|мин|сек|ч)', clean_text)
        if time_match:
            num = int(time_match.group(1))
            unit = time_match.group(2)
            if unit in ['минут', 'мин']:
                seconds = num * 60
                unit_text = f"{num} минут"
            elif unit in ['секунд', 'сек']:
                seconds = num
                unit_text = f"{num} секунд"
            else:
                seconds = num * 3600
                unit_text = f"{num} часов"
            match = re.search(r'напомни\s*(.*?)\s*через', clean_text)
            if match:
                reminder_text = match.group(1).strip()
            else:
                if "напомни" in clean_text and "через" in clean_text:
                    after_remind = clean_text.split("напомни")[-1].strip()
                    reminder_text = after_remind.split("через")[0].strip()
                else:
                    reminder_text = ""
            reminder_text = reminder_text.replace("мне", "").replace("меня", "").replace("что", "").replace("о том", "").replace("чтобы", "").replace("  ", " ").strip()
            if not reminder_text:
                reminder_text = "событие"
            target_time = time.time() + seconds
            reminders.append(Reminder(reminder_text, target_time))
            natural_responses = [
                f"Запомнила! Через {unit_text} напомню: {reminder_text}",
                f"Окей, {reminder_text} — напомню через {unit_text}",
                f"Принято! {reminder_text} — через {unit_text}",
                f"Договорились! Через {unit_text} скажу: {reminder_text}",
                f"Так и сделаю! {reminder_text} через {unit_text}"
            ]
            speak_local(random.choice(natural_responses), text)
            return True
        time_match = re.search(r'в\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', clean_text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            second = int(time_match.group(3)) if time_match.group(3) else 0
            now = datetime.datetime.now()
            target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
            if target < now:
                target += datetime.timedelta(days=1)
            match = re.search(r'напомни\s*(.*?)\s*в', clean_text)
            if match:
                reminder_text = match.group(1).strip()
            else:
                reminder_text = ""
            reminder_text = reminder_text.replace("мне", "").replace("меня", "").replace("что", "").replace("о том", "").replace("чтобы", "").replace("  ", " ").strip()
            if not reminder_text:
                reminder_text = "событие"
            reminders.append(Reminder(reminder_text, target.timestamp()))
            natural_responses = [
                f"Хорошо! Напомню {reminder_text} в {target.strftime('%H:%M')}",
                f"Принято! В {target.strftime('%H:%M')} скажу: {reminder_text}",
                f"Окей, {reminder_text} — в {target.strftime('%H:%M')}",
                f"Запомнила! {reminder_text} в {target.strftime('%H:%M')}"
            ]
            speak_local(random.choice(natural_responses), text)
            return True
        speak_local("Когда напомнить? Скажи, например: 'напомни попить воды через 20 секунд' или 'напомни позвонить в 15:30'", text)
        return True

    if "отмени таймер" in text or "отмени напоминание" in text:
        timers.clear()
        reminders.clear()
        speak_local("Все таймеры и напоминания отменены", text)
        return True

    # ========== РАСШИРЕННЫЕ КОМАНДЫ ЭКОНОМИИ ==========
    eco_off_phrases = [
        "выключи режим экономии", "плавность", "красивый режим", "выключить режим экономии",
        "отключи экономию", "полный режим", "максимальный режим", "красивый интерфейс",
        "отключи режим экономии", "выключи экономию", "включи плавность", "красивая анимация",
        "выключи экономию", "отключи энергосбережение", "выключи энергосбережение", "плавность"
    ]
    if any(phrase in text for phrase in eco_off_phrases):
        eco_mode = False
        memory.update_settings(eco_mode=False)
        speak_local("Режим энергосбережения выключен. Количество частиц восстановлено.", text)
        return True

    eco_on_phrases = [
        "включи режим экономии", "энергосбережение", "режим экономии", "экономный режим",
        "включить режим экономии", "эко режим", "экономия", "сбережение энергии",
        "включи экономию", "режим энергосбережения", "энергосберегающий режим",
        "сэкономь энергию", "экономь", "тихий режим", "слабый режим", "экономия", "экономный"
    ]
    if any(phrase in text for phrase in eco_on_phrases):
        eco_mode = True
        memory.update_settings(eco_mode=True)
        speak_local("Режим энергосбережения включён. Количество частиц уменьшено до 500.", text)
        return True
    
        # ========== КОМАНДЫ КОЛИЧЕСТВА ЧАСТИЦ ==========
    if "уменьши количество частиц" in text or "меньше частиц" in text or "уменьши частицы" in text:
        custom_particle_count = max(100, PARTICLE_COUNT // 2)
        speak_local(f"Количество частиц уменьшено до {custom_particle_count}", text)
        return True
    if "верни количество частиц" in text or "сбрось частицы" in text or "нормальное количество частиц" in text:
        custom_particle_count = None
        speak_local("Количество частиц восстановлено до стандартного", text)
        return True

    # ========== РАСШИРЕННЫЕ КОМАНДЫ ЦВЕТА ==========
    color_map = {
        "красн": ([255, 30, 30], ["красный", "красным", "красную", "красного", "красная", "красный цвет"]),
        "син": ([30, 80, 255], ["синий", "синим", "синюю", "синего", "синяя", "голубой", "лазурный", "синий цвет"]),
        "зелен": ([40, 255, 40], ["зеленый", "зеленым", "зеленую", "зеленого", "зеленая", "изумрудный", "зеленый цвет", "зелёный", "зелёным", "зелёную", "зелёного", "зелёная"]),
        "жёлт": ([255, 255, 0], ["желтый", "желтым", "желтую", "желтого", "желтая", "золотой", "янтарный", "жёлтый", "жёлтым", "жёлтую", "жёлтого", "жёлтая", "желтый цвет"]),
        "бел": ([255, 255, 255], ["белый", "белым", "белую", "белого", "белая", "светлый", "белый цвет"]),
        "стандарт": ([90, 95, 130], ["стандартный", "обычный", "по умолчанию", "нормальный", "обычный цвет", "стандартный цвет"])
    }
    color_info_phrases = [
        "какие цвета", "какие цвета ты умеешь", "какие цвета есть", "какие цвета поддерживаешь",
        "какие цвета можно", "какие цвета доступны", "какие цвета ты знаешь", "какие цвета интерфейса",
        "цвета которые ты умеешь", "цвета которые можешь", "какие у тебя цвета", "какие цвета можешь"
    ]
    if any(phrase in text for phrase in color_info_phrases):
        colors_list = ["красный", "синий", "зеленый", "желтый", "белый", "стандартный"]
        colors_text = ", ".join(colors_list)
        speak_local(f"Я умею менять цвет на {colors_text}. Скажи, например, 'смени цвет на синий'", text)
        return True

    for color_key, (rgb, color_names) in color_map.items():
        for color_name in color_names:
            color_phrases = [
                f"смени цвет на {color_name}",
                f"сделай цвет {color_name}",
                f"цвет {color_name}",
                f"{color_name} цвет",
                f"поменяй цвет на {color_name}",
                f"поменяй на {color_name}",
                f"установи {color_name} цвет",
                f"поставь {color_name} цвет",
                f"сделай {color_name}",
                f"стань {color_name}",
                f"переключи на {color_name}",
                f"измени цвет на {color_name}",
                f"поменяй цвет {color_name}",
                f"смени на {color_name}"
            ]
            if any(phrase in text for phrase in color_phrases) or color_name in text:
                base_color[0], base_color[1], base_color[2] = rgb
                memory.update_settings(color=base_color.copy())
                color_display = color_names[0]
                global_r, global_g, global_b = rgb
                speak_local(f"Цвет изменен на {color_display}", text)
                return True
            
                # ========== ЗАВЕРШЕНИЕ ЗВОНКА ==========
    hangup_phrases = [
        "положить трубку", "завершить звонок", "отбой", "закончить разговор",
        "закончи звонок", "завершить", "отбой звонка", "сбросить вызов", "стоп звонок"
    ]
    if any(phrase in text for phrase in hangup_phrases) and call_manager and call_manager.is_on_call:
        if current_call_id:
            send_hangup(current_call_id)
        call_manager.end_call()
        is_calling = False
        current_call_id = None
        speak_local("Звонок завершён", text)
        return True

    # ========== КОМАНДЫ ВРЕМЕНИ ==========
    time_phrases = [
        "время", "который час", "сколько времени", "час", "текущее время",
        "покажи время", "скажи время", "какое время", "сколько сейчас"
    ]
    if any(phrase in text for phrase in time_phrases):
        now = datetime.datetime.now().strftime('%H:%M')
        speak_local(f"Сейчас {now}", text)
        return True
    
    # ========== УЗНАТЬ СВОЙ DEVICE ID ==========
    if any(phrase in text for phrase in ["какой у меня айди", "мой айди", "мой device id", "какой мой id", "скажи мой айди", "узнай айди", "мой id"]):
        speak_local(f"Ваш идентификатор устройства: {MY_DEVICE_ID}", text)
        return True
    
    # ========== УЗНАТЬ ID КОНТАКТА ==========
    if re.search(r'(какой\s+айди|какой\s+id|узнай\s+айди|скажи\s+айди)\s+(у\s+)?(контакта\s+)?', text, re.IGNORECASE):
        # Извлекаем имя контакта
        match = re.search(r'(?:айди|id)\s+(?:у\s+)?(?:контакта\s+)?([а-яё]+)', text, re.IGNORECASE)
        if not match:
            speak_local("Уточните, пожалуйста, имя контакта.", text)
            return True
        name = match.group(1).strip().lower()
        # Поиск контакта с помощью улучшенной функции
        found = find_contact_by_name(name)
        if found:
            speak_local(f"Идентификатор контакта {found.name}: {found.id}", text)
        else:
            speak_local(f"Контакт с именем {name} не найден.", text)
        return True

    # ========== КОМАНДЫ ЮТУБ ==========
    youtube_phrases = [
        "ютуб", "youtube", "открой ютуб", "включи ютуб", "запусти ютуб",
        "открыть ютуб", "пойти на ютуб", "зайди на ютуб", "видеохостинг"
    ]
    if any(phrase in text for phrase in youtube_phrases):
        webbrowser.open("https://youtube.com")
        speak_local("Открываю Ютуб", text)
        return True

    # ========== КОМАНДЫ СООБЩЕНИЯ ==========
    if any(phrase in text for phrase in MESSAGE_PHRASES):
        print("🔹 ВОШЛИ в блок сообщений")
        reset_dialog_flags()
        if waiting_for_message_contact or waiting_for_message_text or waiting_for_message_confirm:
            speak_local("Сначала закончите текущее сообщение", text)
            return True
        reset_shapes()

        sorted_phrases = sorted(MESSAGE_PHRASES, key=len, reverse=True)
        name_match = None
        for phrase in sorted_phrases:
            if phrase in text:
                name_part = text.split(phrase, 1)[-1].strip()
                if name_part:
                    for noise in ["сообщение", "смс", "текст"]:
                        if name_part.startswith(noise):
                            name_part = name_part[len(noise):].strip()
                            break
                    name_match = name_part
                    break

        print(f"  [AFTER LOOP] name_match = '{name_match}' (type: {type(name_match)})")

        if name_match and name_match.strip():
            print("  === ВЕТКА if (есть имя) ===")
            # Разбиваем на первое слово (имя) и остальное (текст)
            words = name_match.split(maxsplit=1)
            if not words:
                speak_local("Не удалось распознать имя получателя.", text)
                return True
            first_word = words[0]
            msg_text = words[1] if len(words) > 1 else None

            # --- ПОИСК КОНТАКТА (улучшенный) ---
            found_contact = find_contact_by_name(first_word)
            if found_contact:
                if msg_text:
                    if send_message(found_contact.id, msg_text):
                        speak_local(f"Сообщение для {found_contact.name} отправлено: {msg_text}", text)
                    else:
                        speak_local("Не удалось отправить сообщение. Проверьте связь.", text)
                    return True
                else:
                    waiting_for_message_text = True
                    temp_message_contact = found_contact
                    speak_local(f"Что отправить {found_contact.name}?", text)
                    return True
            else:
                speak_local(f"Контакт {first_word} не найден. Скажите имя ещё раз или добавьте контакт.", text)
                return True
        else:
            print("  === ВЕТКА else (нет имени) ===")
            print("  ВКЛЮЧАЕМ КОНВЕРТ")
            waiting_for_message_contact = True
            is_envelope = True
            print(f"  is_envelope = {is_envelope}")
            last_shape_state = None
            speak_local(random.choice(MESSAGE_QUESTIONS), text)
            print("  Возвращаем True")
            return True

    # ========== КОМАНДЫ ПРОЧТЕНИЯ СООБЩЕНИЙ ==========
    if "прочитай сообщения" in text or "покажи сообщения" in text or "новые сообщения" in text:
        reset_dialog_flags()
        if incoming_messages:
            waiting_for_message_read = True
            last_incoming_message = incoming_messages[-1]
            speak_local(f"У вас {len(incoming_messages)} непрочитанных сообщений. Прочитать первое от {last_incoming_message['sender_id']}?", text)
        else:
            speak_local("Нет новых сообщений", text)
        return True

    # ========== КОМАНДЫ КОНТАКТОВ ==========
    if any(phrase in text for phrase in OPEN_CONTACTS_PHRASES):
        reset_dialog_flags()
        reset_shapes()
        if not is_contacts:
            is_contacts = True
            logger.info("🔄 Активирован режим: контакты (открытие)")
            init_contact_shapes()
            last_shape_state = None
        if contacts_manager.contacts:
            names = ", ".join([c.name for c in contacts_manager.contacts])
            speak_local(f"Контакты: {names}. Что хотите сделать?", text)
        else:
            speak_local("Контактов пока нет. Скажите 'добавь контакт'", text)
        return True

    if "добавь контакт" in text or "добавить контакт" in text:
        reset_dialog_flags()
        reset_shapes()
        if not is_contacts:
            is_contacts = True
            init_contact_shapes()
        waiting_for_contact_name = True
        speak_local("Назовите имя контакта", text)
        return True

    if "удали контакт" in text or "удалить контакт" in text:
        reset_dialog_flags()
        reset_shapes()
        # Пытаемся извлечь имя после фразы "удали контакт"
        name_match = None
        for phrase in ["удали контакт", "удалить контакт"]:
            if phrase in text:
                name_part = text.split(phrase, 1)[-1].strip()
                if name_part:
                    name_match = name_part
                    break
        if name_match and name_match.strip():
            # --- ПОИСК КОНТАКТА (улучшенный) ---
            contact = find_contact_by_name(name_match)
            if contact:
                contacts_manager.remove_contact(contact.id)
                # Удаляем форму контакта из contact_shapes
                for i, shape in enumerate(contact_shapes):
                    if shape['id'] == contact.id:
                        del contact_shapes[i]
                        break
                distribute_particles_to_contacts()
                # Если режим контактов не активен – активируем для обновления
                if not is_contacts:
                    is_contacts = True
                    init_contact_shapes()
                speak_local(f"Контакт {contact.name} удалён", text)
                last_command_time = time.time()
            else:
                speak_local(f"Контакт {name_match} не найден", text)
            return True
        else:
            # Если имени нет – переходим в режим ожидания
            if not is_contacts:
                is_contacts = True
                init_contact_shapes()
            waiting_for_contact_delete = True
            speak_local("Назовите имя контакта, который хотите удалить", text)
            return True

    # ========== КОМАНДЫ ЗВОНКА ==========
    if any(phrase in text for phrase in CALL_PHRASES):
        reset_dialog_flags()
        reset_shapes()
        name_match = None
        for phrase in CALL_PHRASES:
            if phrase in text:
                name_part = text.split(phrase, 1)[-1].strip()
                if name_part:
                    name_match = name_part
                    break
        if name_match and name_match.strip():
            if len(name_match) <= 2 and name_match in ["ть", "т", "те", "ти"]:
                name_match = None
            else:
                # --- ПОИСК КОНТАКТА (улучшенный) ---
                contact = find_contact_by_name(name_match)
                if contact:
                    target_id = contact.id
                    if call_manager and not call_manager.is_on_call:
                        if call_manager.start_call(target_id):
                            send_call_request(target_id)
                            is_calling = True
                            last_shape_state = None
                            is_active_session = True
                            last_command_time = time.time()
                            speak_local(f"Звоню {contact.name}...", text)
                            return True
                        else:
                            speak_local("Не удалось начать звонок. Возможно, вы уже разговариваете.", text)
                            return True
                    else:
                        speak_local("Уже в разговоре. Завершите текущий звонок.", text)
                        return True
                else:
                    speak_local(f"Контакт {name_match} не найден.", text)
                    return True
        # Если имени нет, спрашиваем
        waiting_for_call_name = True
        is_calling = True
        last_shape_state = None
        is_active_session = True
        last_command_time = time.time()
        speak_local(random.choice(CALL_QUESTIONS), text)
        return True

    # ========== НАСТРОЙКА ЧУВСТВИТЕЛЬНОСТИ МИКРОФОНА ==========
    if "быстрая реакция" in text or "мгновенная реакция" in text:
        if global_recognizer:
            global_recognizer.pause_threshold = 0.3
            global_recognizer.non_speaking_duration = 0.3
            global_recognizer.phrase_threshold = 0.4
            speak_local("Установлена быстрая реакция. Говорите без пауз.", text)
        else:
            speak_local("Микрофон ещё не готов.", text)
        return True

    if "медленная речь" in text or "я говорю медленно" in text or "я делаю паузы" in text:
        if global_recognizer:
            global_recognizer.pause_threshold = 0.8
            global_recognizer.non_speaking_duration = 0.6
            global_recognizer.phrase_threshold = 0.6
            speak_local("Установлена медленная реакция. Буду ждать дольше.", text)
        else:
            speak_local("Микрофон ещё не готов.", text)
        return True

    if "средняя чувствительность" in text or "стандартная чувствительность" in text:
        if global_recognizer:
            global_recognizer.pause_threshold = 0.5
            global_recognizer.non_speaking_duration = 0.4
            global_recognizer.phrase_threshold = 0.5
            speak_local("Установлена средняя чувствительность.", text)
        else:
            speak_local("Микрофон ещё не готов.", text)
        return True

    if "покажи настройки микрофона" in text or "какие настройки микрофона" in text:
        if global_recognizer:
            settings_text = (f"Настройки микрофона:\n"
                            f"• Пауза: {global_recognizer.pause_threshold} сек\n"
                            f"• Тишина: {global_recognizer.non_speaking_duration} сек\n"
                            f"• Мин. фраза: {global_recognizer.phrase_threshold} сек\n"
                            f"• Порог шума: {global_recognizer.energy_threshold}")
            speak_local(settings_text, text)
        else:
            speak_local("Микрофон ещё не готов.", text)
        return True

    return False

# Локальные ответы на частые вопросы о функциональности
LOCAL_FAQ = [
    (r"как позвонить|как сделать звонок|как набрать номер", 
     "Чтобы позвонить, скажите: «Спектр, позвони [имя контакта]» или «Спектр, позвони по номеру [цифры]»."),
    (r"как отправить сообщение|как написать сообщение|как послать смс",
     "Чтобы отправить сообщение, скажите: «Спектр, напиши [имя контакта]» и продиктуйте текст."),
    (r"как добавить контакт|как сохранить контакт",
     "Чтобы добавить контакт, скажите: «Спектр, добавь контакт». Затем назовите имя и продиктуйте ID (цифры)."),
    (r"как удалить контакт|как убрать контакт",
     "Чтобы удалить контакт, скажите: «Спектр, удали контакт» и назовите имя."),
    (r"как прочитать сообщение|как посмотреть сообщения|новые сообщения",
     "Новые сообщения я читаю автоматически. Скажите «Спектр, прочитай сообщения»."),
    (r"как изменить цвет|как сменить цвет",
     "Чтобы изменить цвет, скажите: «Спектр, смени цвет на [красный, синий, зелёный, жёлтый, белый или стандартный]»."),
    (r"как изменить скорость речи|как говорить быстрее|как говорить медленнее",
     "Чтобы изменить скорость речи, скажите: «Спектр, говори быстрее» или «Спектр, говори медленнее». Можно также указать процент: «Спектр, скорость 30%»."),
    (r"как изменить громкость|сделай громче|сделай тише",
     "Чтобы изменить громкость, скажите: «Спектр, говори громче» или «Спектр, говори тише». Можно указать процент: «Спектр, громкость 50%»."),
    (r"что ты умеешь|какие команды|твои возможности|чем можешь помочь",
     "Я умею: звонить, отправлять сообщения, управлять контактами, ставить таймеры и напоминания, менять цвет интерфейса, скорость и громкость речи, открывать YouTube, а также отвечать на разные вопросы через нейросеть. Скажите, что нужно сделать."),
]

def answer_faq(text):
    """Проверяет, не является ли текст часто задаваемым вопросом, и возвращает ответ или None."""
    text_lower = text.lower()
    for pattern, answer in LOCAL_FAQ:
        if re.search(pattern, text_lower):
            return answer
    return None

# ============================================
# 🎤 ОБРАБОТКА ГОЛОСА
# ============================================
def voice_callback(recognizer, audio):
    global is_thinking, is_recording, is_active_session, last_command_time
    global is_calling, is_envelope, is_contacts, is_loading, is_warning, is_processing
    global is_speaking
    global waiting_for_contact_name, waiting_for_contact_id, waiting_for_contact_confirm
    global waiting_for_contact_delete, temp_contact_name, temp_contact_id
    global contact_shapes, contacts_manager
    global call_target_id, message_target_id
    global waiting_for_message_contact, waiting_for_message_text, waiting_for_message_confirm
    global temp_message_contact, temp_message_text
    global waiting_for_message_read, waiting_for_message_reply, temp_reply_sender, last_incoming_message, incoming_messages
    global last_shape_state
    global stop_requested
    global waiting_for_call_answer, pending_call_request

    is_recording = False
    try:
        raw_text = recognizer.recognize_google(audio, language="ru").lower()
        logger.info(f"🎤 Распознано: '{raw_text}'")

        # Если мы в звонке, игнорируем всё, кроме команд завершения
        if call_manager and call_manager.is_on_call:
            if not any(phrase in raw_text for phrase in ["положить трубку", "завершить звонок", "отбой", "стоп", "хватит"]):
                return

        logger.debug(f"🎤 Состояния до обработки: is_calling={is_calling}, is_envelope={is_envelope}, is_contacts={is_contacts}, last_shape_state={last_shape_state}")

        # Используем глобальный список пробуждающих слов
        names = WAKE_WORDS

        # Стоп-команды в первую очередь (всегда)
        stop_words = ["стоп", "хватит", "молчи", "замолчи", "тихо", "усни", "отдыхай", "отдыха"]
        if any(word in raw_text for word in stop_words):
            logger.info(f"🛑 Стоп-команда во время речи: '{raw_text}'")
            stop_requested = True
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                pygame.mixer.stop()
            except:
                pass
            is_thinking = False
            is_speaking = False
            is_active_session = False
            is_calling = False
            is_envelope = False
            is_contacts = False
            is_loading = False
            is_warning = False
            is_processing = False
            reset_dialog_flags()
            logger.info("✅ Состояния и диалоговые флаги сброшены")
            return
        else:
            # Если это не стоп-команда, сбрасываем флаг отмены (чтобы следующие команды не отменялись)
            stop_requested = False

        # ========== НОВЫЙ БЛОК: приоритетные команды переключения режимов ==========
        priority_switch_commands = (
            MESSAGE_PHRASES + CALL_PHRASES + OPEN_CONTACTS_PHRASES + 
            CLOSE_CONTACTS_PHRASES + ["прочитай сообщения", "покажи сообщения", "новые сообщения"]
        )
        if any(phrase in raw_text for phrase in priority_switch_commands):
            if (waiting_for_message_contact or waiting_for_message_text or waiting_for_message_confirm or
                waiting_for_contact_name or waiting_for_contact_id or waiting_for_contact_confirm or
                waiting_for_contact_delete or waiting_for_message_reply):
                logger.info("⚡ Приоритетная команда прерывает текущий диалог")
                reset_dialog_flags()

        # Если Спектр говорит, обрабатываем приоритетные команды
        if is_speaking:
            if is_active_session:
                logger.info(f"🎤 Активная сессия, прерываю речь для обработки: '{raw_text}'")
                stop_speaking()
                clean_text = raw_text
                for name in names:
                    clean_text = clean_text.replace(name, "")
                clean_text = clean_text.replace("эй", "").strip()
                if handle_commands(clean_text):
                    return
                is_thinking = True
                is_active_session = True
                try:
                    response = requests.post(ASK_URL, json={"text": clean_text, "device_id": MY_DEVICE_ID, "token": MY_TOKEN}, timeout=10)
                    if response.status_code == 200:
                        answer = response.json().get("answer", "...")
                        # Проверяем, не была ли запрошена стоп-команда во время ожидания ответа
                        if stop_requested:
                            logger.info("Ответ отменён, так как была стоп-команда")
                            stop_requested = False
                        else:
                            speak_local(answer, clean_text)
                    else:
                        if is_active_session:
                            speak_local("Сервер не отвечает.")
                except Exception as e:
                    logger.error(f"❌ Ошибка сервера: {e}")
                finally:
                    is_thinking = False
                return
            else:
                all_commands = CALL_PHRASES + MESSAGE_PHRASES + OPEN_CONTACTS_PHRASES + CLOSE_CONTACTS_PHRASES
                stop_cmds = ["отдыхай", "стоп", "хватит", "молчи", "замолчи", "тихо", "усни"]
                priority_phrases = all_commands + stop_cmds
                if any(phrase in raw_text for phrase in priority_phrases):
                    logger.info(f"⚡ Приоритетная команда во время речи: '{raw_text}'")
                    stop_speaking()
                    clean_text = raw_text
                    for name in names:
                        clean_text = clean_text.replace(name, "")
                    clean_text = clean_text.replace("эй", "").strip()
                    if handle_commands(clean_text):
                        return
                else:
                    return

        # ========== МНОГОШАГОВЫЙ ДИАЛОГ КОНТАКТОВ ==========
        if waiting_for_contact_name:
            name = raw_text
            for n in names:
                name = name.replace(n, "")
            name = name.strip()
            if name:
                temp_contact_name = name
                waiting_for_contact_name = False
                waiting_for_contact_id = True
                speak_local(f"Какой идентификатор у {name}? Скажите цифры", raw_text)
            else:
                speak_local("Не понял имя, попробуйте ещё раз", raw_text)
            return

        if waiting_for_contact_id:
            digit_words = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
            words = raw_text.split()
            digits = []
            for w in words:
                if w in digit_words:
                    digits.append(str(digit_words.index(w)))
                elif w.isdigit():
                    digits.append(w)
            if digits:
                temp_contact_id = "".join(digits)
                waiting_for_contact_id = False
                waiting_for_contact_confirm = True
                speak_local(f"Контакт {temp_contact_name} с идентификатором {temp_contact_id}. Подтвердите: да или нет", raw_text)
            else:
                speak_local("Не удалось распознать идентификатор. Скажите цифры словами, например: один два три", raw_text)
            return

        if waiting_for_contact_confirm:
            if "да" in raw_text or "подтверждаю" in raw_text:
                contact = contacts_manager.add_contact(temp_contact_name, temp_contact_id)
                if contact:
                    add_contact_shape(contact)
                    if not is_contacts:
                        is_contacts = True
                        init_contact_shapes()
                    speak_local(f"Контакт {temp_contact_name} добавлен", raw_text)
                    last_command_time = time.time()
                else:
                    speak_local(f"Контакт с таким идентификатором уже существует", raw_text)
            else:
                speak_local("Добавление отменено", raw_text)
            waiting_for_contact_confirm = False
            temp_contact_name = ""
            temp_contact_id = ""
            return

        if waiting_for_contact_delete:
            name = raw_text
            for n in names:
                name = name.replace(n, "")
            name = name.strip()
            if name:
                # --- ПОИСК КОНТАКТА (улучшенный) ---
                contact = find_contact_by_name(name)
                if contact:
                    contacts_manager.remove_contact(contact.id)
                    for i, shape in enumerate(contact_shapes):
                        if shape['id'] == contact.id:
                            del contact_shapes[i]
                            break
                    distribute_particles_to_contacts()
                    if not is_contacts:
                        is_contacts = True
                        init_contact_shapes()
                    speak_local(f"Контакт {name} удалён", raw_text)
                    last_command_time = time.time()
                else:
                    speak_local(f"Контакт с именем {name} не найден", raw_text)
            else:
                speak_local("Не понял имя, попробуйте ещё раз", raw_text)
            waiting_for_contact_delete = False
            return

        # ========== ДИАЛОГ ОТПРАВКИ СООБЩЕНИЯ ==========
        if waiting_for_message_contact:
            name = raw_text
            for n in names:
                name = name.replace(n, "")
            name = name.strip()
            # --- ПОИСК КОНТАКТА (улучшенный) ---
            contact = find_contact_by_name(name)
            if contact:
                temp_message_contact = contact
                waiting_for_message_contact = False
                waiting_for_message_text = True
                speak_local(f"Что отправить {contact.name}? Диктуйте текст.", raw_text)
            else:
                speak_local(f"Контакт {name} не найден. Скажите имя ещё раз или добавьте контакт.", raw_text)
            return

        if waiting_for_message_text:
            temp_message_text = raw_text
            waiting_for_message_text = False
            waiting_for_message_confirm = True
            speak_local(f"Отправить {temp_message_contact.name}: {temp_message_text}. Подтвердите: да или нет", raw_text)
            return

        if waiting_for_message_confirm:
            if "да" in raw_text or "подтверждаю" in raw_text:
                logger.info(f"✉️ Отправка сообщения для {temp_message_contact.id}: {temp_message_text[:50]}...")
                if send_message(temp_message_contact.id, temp_message_text):
                    start_send_success_animation()
                    speak_local(f"Сообщение для {temp_message_contact.name} отправлено", raw_text)
                else:
                    speak_local("Не удалось отправить сообщение. Проверьте связь.", raw_text)
            else:
                speak_local("Отправка отменена", raw_text)
            waiting_for_message_confirm = False
            temp_message_contact = None
            temp_message_text = ""
            is_envelope = False
            last_shape_state = None
            return

        # ========== ДИАЛОГ ЧТЕНИЯ СООБЩЕНИЙ ==========
        if waiting_for_message_read:
            if "да" in raw_text or "прочитай" in raw_text or "читай" in raw_text or "прочитать" in raw_text:
                msg = last_incoming_message
                speak_local(f"Сообщение от {msg['sender_id']}: {msg['text']}", raw_text)
                if msg in incoming_messages:
                    incoming_messages.remove(msg)
                if incoming_messages:
                    last_incoming_message = incoming_messages[-1]
                    speak_local(f"Ещё {len(incoming_messages)} сообщений. Прочитать следующее?", raw_text)
                else:
                    waiting_for_message_read = False
                    last_incoming_message = None
                    # После прочтения всех сообщений предлагаем ответить на последнее
                    if msg and not waiting_for_message_reply:
                        waiting_for_message_reply = True
                        temp_reply_sender = msg['sender_id']
                        speak_local(f"Ответить {msg['sender_id']}?", raw_text)
            elif "нет" in raw_text or "позже" in raw_text or "стоп" in raw_text:
                speak_local("Хорошо, сообщение сохранено. Скажите 'прочитай сообщения', когда будете готовы.", raw_text)
                waiting_for_message_read = False
            else:
                speak_local("Не понял. Ответьте 'да' или 'нет'", raw_text)
            return

        # ========== ДИАЛОГ ОТВЕТА НА СООБЩЕНИЕ ==========
        if waiting_for_message_reply:
            if "да" in raw_text or "ответить" in raw_text:
                # Переходим в режим отправки сообщения с предзаполненным контактом
                contact = contacts_manager.get_contact_by_id(temp_reply_sender)
                if contact:
                    waiting_for_message_text = True
                    temp_message_contact = contact
                    waiting_for_message_reply = False
                    speak_local(f"Что отправить {contact.name}? Диктуйте текст.", raw_text)
                else:
                    # Если контакт не найден по ID, запрашиваем имя
                    print("  ИМЕНИ НЕТ, ВКЛЮЧАЕМ КОНВЕРТ")
                    waiting_for_message_contact = True
                    waiting_for_message_reply = False
                    is_envelope = True
                    print(f"  is_envelope установлен в {is_envelope}")
                    speak_local("Укажите получателя для ответа.", raw_text)
            elif "нет" in raw_text or "не надо" in raw_text:
                speak_local("Хорошо, не будем отвечать.", raw_text)
                waiting_for_message_reply = False
                temp_reply_sender = None
            else:
                speak_local("Не понял. Ответьте 'да' или 'нет'", raw_text)
            return
        
                # ========== ОТВЕТ НА ВХОДЯЩИЙ ЗВОНОК ==========
        if waiting_for_call_answer:
            answer = raw_text.lower()
            # Слова для подтверждения
            accept_words = ["да", "подтверждаю", "ответить", "конечно", "алло", "принимаю", "ок", "давай", "принято"]
            # Слова для отклонения
            reject_words = ["нет", "отклоняю", "не надо", "не хочу", "отбой", "откажись", "позже", "не сейчас", "отказать"]
            
            if any(word in answer for word in accept_words):
                call_id = pending_call_request[1]
                send_call_answer(call_id, accept=True)
                speak_local("Принимаю звонок", raw_text)
                waiting_for_call_answer = False
                pending_call_request = None
                # is_calling останется True – позже придёт call_started
            elif any(word in answer for word in reject_words):
                call_id = pending_call_request[1]
                send_call_answer(call_id, accept=False)
                speak_local("Звонок отклонён", raw_text)
                waiting_for_call_answer = False
                pending_call_request = None
                is_calling = False   # сбросить визуальный режим, если он был
            else:
                speak_local("Не понял. Скажите 'да', 'подтверждаю' или 'нет', 'отклоняю'", raw_text)
            return

        # ========== ОСНОВНАЯ ЛОГИКА (команды или сервер) ==========
        triggered = any(name in raw_text for name in names)

        temp_text = raw_text
        if triggered:
            for name in names:
                temp_text = temp_text.replace(name, "")
            temp_text = temp_text.replace("эй", "").strip()

        stop_commands = [
            "стоп", "отстань", "молчи", "хватит", "замолчи", "тихо", "усни",
            "отмена", "заткнись", "перестань", "прекрати", "забудь", "отдыхай",
            "отдыха", "отдых", "хвати", "хват", "молча", "замолка", "тиш"
        ]

        is_stop = False
        for cmd in stop_commands:
            if cmd in temp_text or cmd in raw_text:
                is_stop = True
                break
        if temp_text in ["стоп", "хватит", "молчи", "тихо", "усни", "отдыхай", "отдыха", "хвати"]:
            is_stop = True

        if is_stop:
            logger.info(f"🛑 Команда остановки: '{temp_text}'")
            stop_requested = True
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
            except:
                pass
            is_thinking = False
            is_speaking = False
            is_active_session = False
            is_calling = False
            is_envelope = False
            is_contacts = False
            is_loading = False
            is_warning = False
            is_processing = False
            call_target_id = None
            message_target_id = None
            reset_dialog_flags()
            logger.info("✅ Все состояния сброшены, Спектр в режиме ожидания")
            return

        if triggered or is_active_session:
            last_command_time = time.time()
            clean_text = raw_text
            if triggered:
                for name in names:
                    clean_text = clean_text.replace(name, "")
                clean_text = clean_text.replace("эй", "").strip()
                # Дополнительная очистка: убираем лишние пробелы
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()

            if not clean_text and triggered:
                is_active_session = True
                speak_local(random.choice(WAKE_ANSWERS))
                return

            if not clean_text:
                return

            # ---------- 1. Сначала пробуем обработать как локальную команду ----------
            if handle_commands(clean_text):
                is_thinking = False
                return

            # ---------- 2. Если не команда – проверяем локальный FAQ ----------
            faq_answer = answer_faq(clean_text)
            if faq_answer:
                speak_local(faq_answer, clean_text)
                return

            # ---------- 3. Всё остальное отправляем на сервер ----------
            is_thinking = True
            is_active_session = True
            stop_before_request = stop_requested
            try:
                response = requests.post(ASK_URL, json={"text": clean_text, "device_id": MY_DEVICE_ID, "token": MY_TOKEN}, timeout=10)
                if stop_requested or stop_before_request != stop_requested:
                    logger.info("Ответ отменён, так как была стоп-команда во время ожидания")
                    stop_requested = False
                else:
                    if response.status_code == 200:
                        answer = response.json().get("answer", "...")
                        speak_local(answer, clean_text)
                    else:
                        if is_active_session:
                            speak_local("Сервер не отвечает. Попробуйте позже.")
            except requests.exceptions.ConnectionError:
                logger.error("❌ Нет соединения с сервером")
                if is_active_session:
                    speak_local("Нет соединения с сервером. Проверьте интернет.")
            except Exception as e:
                logger.error(f"❌ Ошибка сервера: {e}")
                if is_active_session:
                    speak_local("Произошла ошибка при обращении к серверу.")
            finally:
                is_thinking = False

    except sr.UnknownValueError:
        pass
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

# ============================================
# 🎤 НАСТРОЙКА МИКРОФОНА И ЗАПУСК ФОНОВОГО СЛУШАТЕЛЯ
# ============================================
def setup_mic():
    global global_recognizer
    global_recognizer = sr.Recognizer()
    global_recognizer.dynamic_energy_threshold = False
    global_recognizer.dynamic_energy_ratio = 1.5
    global_recognizer.energy_threshold = 100
    global_recognizer.pause_threshold = 0.3
    global_recognizer.non_speaking_duration = 0.3
    global_recognizer.phrase_threshold = 0.4

    try:
        mic = sr.Microphone()
        logger.info("📡 Калибровка микрофона под фоновый шум (3 секунды)...")
        logger.info("🤫 Пожалуйста, помолчите 3 секунды...")
        with mic as source:
            global_recognizer.adjust_for_ambient_noise(source, duration=3.0)
        logger.info(f"✅ Микрофон откалиброван. Порог шума: {global_recognizer.energy_threshold}")
        logger.info("🎤 Теперь можно говорить!")

        def auto_calibrate_style():
            time.sleep(15)
            if global_recognizer:
                logger.info("🔧 Авто-калибровка: анализирую стиль речи...")
                global_recognizer.pause_threshold = 1.2
                global_recognizer.non_speaking_duration = 1.0
                global_recognizer.phrase_threshold = 0.5
                logger.info(f"✅ Авто-калибровка завершена: пауза={global_recognizer.pause_threshold} сек")
        threading.Thread(target=auto_calibrate_style, daemon=True).start()
        global_recognizer.listen_in_background(mic, voice_callback, phrase_time_limit=10)
    except Exception as e:
        logger.error(f"❌ Микрофон: {e}")

threading.Thread(target=setup_mic, daemon=True).start()

# ============================================
# 🔄 ФОНТОВЫЙ ПРОЦЕСС ПРОВЕРКИ ТАЙМЕРОВ
# ============================================
def timer_checker():
    while True:
        check_timers()
        time.sleep(1)

threading.Thread(target=timer_checker, daemon=True).start()

# ============================================
# 🌐 WebSocket & Pygame
# ============================================
_ws_thread_started = False
_ws_connected = False

def network_listener():
    global is_envelope, is_calling, is_contacts, last_command_time, current_emotion, _ws_thread_started, _ws_connected
    global incoming_messages, waiting_for_message_read, last_incoming_message
    global current_call_id

    if _ws_thread_started:
        return
    _ws_thread_started = True

    def send_ping(ws):
        while _ws_connected:
            try:
                time.sleep(10)
                ws.send(json.dumps({"type": "ping"}))
                logger.debug("📡 WebSocket ping отправлен")
            except:
                break

    while True:
        try:
            ws = websocket.create_connection(
                WS_URL,
                timeout=30,
                ping_interval=10,
                ping_timeout=5
            )
            # Аутентификация WebSocket
            auth_msg = json.dumps({
                "type": "auth",
                "device_id": MY_DEVICE_ID,
                "token": MY_TOKEN
            })
            ws.send(auth_msg)
            global main_ws
            main_ws = ws
            logger.info(f"🔑 Отправлена аутентификация для {MY_DEVICE_ID}")
            _ws_connected = True
            logger.info("🌐 WebSocket соединение установлено")
            ping_thread = threading.Thread(target=send_ping, args=(ws,), daemon=True)
            ping_thread.start()

            while True:
                try:
                    message = ws.recv()
                    if not message:
                        continue
                    data = json.loads(message)
                    if data.get("type") == "command":
                        val = data.get("value")
                        if val == "envelope":
                            is_envelope, is_calling, is_contacts = True, False, False
                            logger.info("📨 Получена команда: envelope")
                        if val == "incoming_call":
                            is_calling, is_envelope, is_contacts = True, False, False
                            logger.info("📞 Получена команда: incoming_call")
                        last_command_time = time.time()
                    elif data.get("type") == "message":
                        msg = {
                            "sender_id": data.get("sender_id", "Неизвестный"),
                            "text": data.get("text", ""),
                            "timestamp": data.get("timestamp", datetime.datetime.now().strftime("%H:%M:%S"))
                        }
                        logger.info(f"📨 Получено сообщение от {msg['sender_id']}: {msg['text'][:50]}...")
                        incoming_messages.append(msg)
                        notify_new_message()

                    elif data.get("type") == "incoming_call":
                        from_id = data.get("from_id")
                        call_id = data.get("call_id")
                        current_call_id = call_id
                        notify_incoming_call(from_id, call_id)

                    elif data.get("type") == "call_started":
                        call_id = data.get("call_id")
                        audio_ws_url = data.get("audio_ws_url")
                        current_call_id = call_id
                        if call_manager:
                            call_manager.start_audio(audio_ws_url)

                    elif data.get("type") == "call_ringing":
                        call_id = data.get("call_id")
                        current_call_id = call_id
                        logger.info(f"📞 Звонок инициирован, call_id={call_id}")

                    elif data.get("type") == "call_ended":
                        if call_manager and call_manager.is_on_call:
                            call_manager.end_call()
                            speak_local("Звонок завершён собеседником", save_user_text="")

                except websocket.WebSocketConnectionClosedException:
                    logger.error("❌ Соединение закрыто")
                    break
                except json.JSONDecodeError as e:
                    logger.error(f"❌ Некорректный JSON: {message[:100]}... Ошибка: {e}")
                    continue
                except Exception as e:
                    logger.error(f"❌ Ошибка WebSocket: {e}")
                    if 'ws' in locals():
                        try:
                            ws.close()
                        except:
                            pass
                    _ws_connected = False
                    break
        except Exception as e:
            logger.error(f"❌ Не удалось подключиться к WebSocket: {e}")
            _ws_connected = False
            time.sleep(5)
threading.Thread(target=network_listener, daemon=True).start()

def on_call_ended():
    logger.info("on_call_ended: начало")
    global is_calling, current_call_id
    is_calling = False
    current_call_id = None
    logger.info("Звонок завершён, визуал выключен")

# ============================================
# 🎨 PYGAME ИНИЦИАЛИЗАЦИЯ И ОСНОВНОЙ ЦИКЛ
# ============================================
pygame.init()
screen = pygame.display.set_mode((600, 600))
pygame.display.set_caption("SPECTRUM OS")
clock = pygame.time.Clock()
fade_surface = pygame.Surface((600, 600))
fade_surface.fill((0, 0, 0))
fade_surface.set_alpha(70)

particles = []
for i in range(PARTICLE_COUNT):
    angle = random.uniform(0, 2 * math.pi)
    radius = random.uniform(0, 95)
    particles.append({
        "x": random.randint(0, 600), "y": random.randint(0, 600),
        "vx": 0, "vy": 0,
        "offset_x": radius * math.cos(angle), "offset_y": radius * math.sin(angle),
        "accel": random.uniform(0.001, 0.006), "friction": random.uniform(0.96, 0.98),
        "size": random.choice([1, 2]), "target_rand": random.random(),
        "contact_id": None, "contact_idx": 0, "contact_total": 0,
        "contact_shape_type": None, "contact_center": None
    })

# Регистрация устройства при старте
register_device()

call_manager = CallManager(SERVER_IP, MY_DEVICE_ID, MY_TOKEN)
call_manager.on_call_ended = on_call_ended

fetch_new_messages()

running = True
while running:
    try:
        screen.blit(fade_surface, (0, 0))
        t = time.time()
        dt = clock.get_time() / 1000.0
        visual_active = is_thinking or is_speaking

        if is_speaking:
            last_command_time = t

        if is_active_session and not visual_active and (t - last_command_time > SESSION_TIMEOUT):
            if is_speaking:
                last_command_time = t
                logger.info("⏰ Таймаут продлён (идёт речь)")
            elif not (is_calling or is_envelope or is_contacts):
                is_active_session = False
                logger.info("⏰ Таймаут сессии, режимы сброшены")
            else:
                last_command_time = t
                logger.info("⏰ Таймаут сессии продлён из-за активного режима")

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                logger.info("Получен сигнал QUIT, завершаем программу")
                running = False

        if not visual_active and not (is_calling or is_envelope or is_contacts or is_loading or is_warning or is_processing or is_active_session):
            cloud_target_x = 300 + 150 * math.sin(t * 0.7)
            cloud_target_y = 300 + 100 * math.cos(t * 0.5)
            cloud_center_x += (cloud_target_x - cloud_center_x) * 0.08
            cloud_center_y += (cloud_target_y - cloud_center_y) * 0.08

        if custom_particle_count is not None:
            active_p_count = custom_particle_count
        else:
            active_p_count = 500 if eco_mode else PARTICLE_COUNT

        current_shape_state = None
        if is_calling:
            current_shape_state = "call"
        elif is_envelope:
            current_shape_state = "envelope"
        elif is_contacts:
            current_shape_state = "contacts"

        if current_shape_state != last_shape_state:
            logger.info(f"🔄 Смена формы: {last_shape_state} -> {current_shape_state}")
            if last_shape_state is not None:
                for i in range(min(active_p_count, len(particles))):
                    particles[i]['old_target_x'] = particles[i].get('target_x', particles[i]['x'])
                    particles[i]['old_target_y'] = particles[i].get('target_y', particles[i]['y'])
                    particles[i]['transition'] = 1.0
                logger.info(f"📌 Переход: установлен transition для {min(active_p_count, len(particles))} частиц")
            last_shape_state = current_shape_state

        for i in range(active_p_count):
            p = particles[i]
            pos = p["target_rand"]

            if is_calling:
                w, h, sx, sy = 160, 280, 220, 160
                if i < active_p_count * 0.8:
                    side = i % 4
                    if side == 0: tx, ty = sx + pos*w, sy
                    elif side == 1: tx, ty = sx + w, sy + pos*h
                    elif side == 2: tx, ty = sx + pos*w, sy + h
                    else: tx, ty = sx, sy + pos*h
                else:
                    btn_r = 15
                    bx, by = sx + w//2, sy + h - 35
                    ang = pos * 2 * math.pi
                    tx, ty = bx + math.cos(ang) * btn_r, by + math.sin(ang) * btn_r
                fric, acc, noise = 0.92, 0.07, 1.8
                p['target_x'] = tx
                p['target_y'] = ty

            elif is_envelope:
                sx, sy, w, h = 180, 225, 240, 150
                part = i % 6
                if part == 0: tx, ty = sx + pos*w, sy
                elif part == 1: tx, ty = sx + w, sy + pos*h
                elif part == 2: tx, ty = sx + pos*w, sy + h
                elif part == 3: tx, ty = sx, sy + pos*h
                elif part == 4: tx, ty = sx + pos*120, sy + pos*75
                else: tx, ty = sx + 240 - pos*120, sy + pos*75
                fric, acc, noise = 0.92, 0.07, 1.8
                p['target_x'] = tx
                p['target_y'] = ty

            elif is_contacts:
                if p['contact_id'] is not None:
                    contact = contacts_manager.get_contact_by_id(p['contact_id'])
                    if contact:
                        shape_data = next((s for s in contact_shapes if s['id'] == contact.id), None)
                        if shape_data:
                            cx = contacts_ring_center[0] + math.cos(shape_data['angle']) * contacts_ring_radius
                            cy = contacts_ring_center[1] + math.sin(shape_data['angle']) * contacts_ring_radius
                            rotation = t * 0.5
                            tx, ty = get_contact_shape_point(
                                shape_data['shape_type'],
                                (cx, cy),
                                p['contact_idx'],
                                p['contact_total'],
                                rotation
                            )
                        else:
                            angle = pos * 2 * math.pi
                            tx = contacts_ring_center[0] + math.cos(angle) * contacts_ring_radius
                            ty = contacts_ring_center[1] + math.sin(angle) * contacts_ring_radius
                    else:
                        angle = pos * 2 * math.pi
                        tx = contacts_ring_center[0] + math.cos(angle) * contacts_ring_radius
                        ty = contacts_ring_center[1] + math.sin(angle) * contacts_ring_radius
                else:
                    angle = pos * 2 * math.pi
                    tx = contacts_ring_center[0] + math.cos(angle) * contacts_ring_radius
                    ty = contacts_ring_center[1] + math.sin(angle) * contacts_ring_radius
                fric, acc, noise = 0.92, 0.08, 1.8
                p['target_x'] = tx
                p['target_y'] = ty

            elif is_speaking:
                pulse = math.sin(t * 15) * 0.3 + 0.8
                tx, ty = 300 + p["offset_x"] * 0.6 * pulse, 300 + p["offset_y"] * 0.6 * pulse
                fric, acc, noise = 0.85, 0.15, 4
                p.pop('old_target_x', None)
                p.pop('old_target_y', None)
                p.pop('transition', None)

            elif is_thinking:
                a = t * 5 + pos * 6.28
                r = 100 + 20 * math.sin(t * 10 + pos * 10)
                tx, ty = 300 + math.cos(a) * r, 300 + math.sin(a) * r
                fric, acc, noise = 0.85, 0.12, 2
                p.pop('old_target_x', None)
                p.pop('old_target_y', None)
                p.pop('transition', None)

            elif is_active_session:
                tx, ty = 300 + p["offset_x"] * 0.8, 300 + p["offset_y"] * 0.8
                fric, acc, noise = 0.9, 0.1, 2
                p.pop('old_target_x', None)
                p.pop('old_target_y', None)
                p.pop('transition', None)

            elif is_loading:
                angle = pos * 2 * math.pi
                r = 150 + 20 * math.sin(t * 10 + pos * 20)
                tx, ty = 300 + math.cos(angle) * r, 300 + math.sin(angle) * r
                fric, acc, noise = 0.92, 0.08, 2.5
                p.pop('old_target_x', None)
                p.pop('old_target_y', None)
                p.pop('transition', None)

            else:
                if random.random() < 0.01:
                    angle = random.uniform(0, 2 * math.pi)
                    dist = random.uniform(0, 100)
                    p["offset_x"] = dist * math.cos(angle)
                    p["offset_y"] = dist * math.sin(angle)
                cloud_target_x = 300 + 200 * math.sin(t * 1.0)
                cloud_target_y = 300 + 150 * math.sin(t * 2.0)
                cloud_center_x += (cloud_target_x - cloud_center_x) * 0.08
                cloud_center_y += (cloud_target_y - cloud_center_y) * 0.08
                tx = cloud_center_x + p["offset_x"]
                ty = cloud_center_y + p["offset_y"]
                fric, acc, noise = p["friction"], p["accel"] * 1.2, 2.0
                p.pop('old_target_x', None)
                p.pop('old_target_y', None)
                p.pop('transition', None)

            if 'transition' in p and p['transition'] > 0:
                old_tx = p.get('old_target_x', tx)
                old_ty = p.get('old_target_y', ty)
                tx = old_tx * p['transition'] + tx * (1 - p['transition'])
                ty = old_ty * p['transition'] + ty * (1 - p['transition'])
                p['transition'] = max(0.0, p['transition'] - 0.060)
                if p['transition'] <= 0:
                    p.pop('old_target_x', None)
                    p.pop('old_target_y', None)
                    p.pop('transition', None)

            p["vx"] = (p["vx"] + (tx - p["x"]) * acc) * fric
            p["vy"] = (p["vy"] + (ty - p["y"]) * acc) * fric
            p["x"] += p["vx"] + random.uniform(-noise, noise)
            p["y"] += p["vy"] + random.uniform(-noise, noise)

        # Определение цвета
        if red_override is not None:
            target_c = red_override
        elif is_warning:
            target_c = (255, 30, 30)
        elif is_loading:
            target_c = (255, 215, 0)
        elif is_thinking:
            target_c = (200, 100, 255)
        elif is_calling or is_envelope or is_contacts:
            target_c = (240, 240, 255)
        elif is_speaking:
            em = {"happy": (100, 255, 150), "sad": (100, 150, 255), "surprised": (255, 255, 100), "angry": (255, 50, 50)}
            target_c = em.get(current_emotion, (255, 255, 255))
        else:
            target_c = base_color

        global_r += (target_c[0] - global_r) * 0.1
        global_g += (target_c[1] - global_g) * 0.1
        global_b += (target_c[2] - global_b) * 0.1

        if is_contacts:
            update_contact_shapes(dt)

        for i in range(active_p_count):
            p = particles[i]
            pygame.draw.circle(screen, (int(global_r), int(global_g), int(global_b)), (int(p["x"]), int(p["y"])), p["size"])

        pygame.display.flip()
        clock.tick(60)

    except Exception as e:
        logger.exception(f"Критическая ошибка в основном цикле: {e}")
        import traceback
        traceback.print_exc()
        time.sleep(1)   # чтобы не зациклиться при ошибке

# Закрываем PyAudio перед выходом
# Даём фоновым потокам время завершиться
time.sleep(0.5)
# Закрываем PyAudio перед выходом
_close_pyaudio()
pygame.quit()