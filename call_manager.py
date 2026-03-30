# call_manager.py
import threading
import time
import pyaudio
import websocket
import logging
import json

logger = logging.getLogger("SpectrumAssistant")

class CallManager:
    def __init__(self, server_ip, my_device_id, my_token, send_audio=True):
        self.server_ip = server_ip
        self.my_device_id = my_device_id
        self.my_token = my_token
        self.send_audio = send_audio
        self.is_on_call = False
        self.call_ws = None
        self.audio_input_stream = None
        self.audio_output_stream = None
        self.pyaudio_instance = None
        self.send_thread = None
        self.running = False
        self.on_call_ended = None
        self.lock = threading.Lock()
        # Буфер для накопления пакетов
        self.packet_buffer = []
        self.buffer_lock = threading.Lock()

    def start_call(self, target_id):
        if self.is_on_call:
            logger.warning("Уже в звонке")
            return False
        self.target_id = target_id
        return True

    def start_audio(self, audio_ws_url):
        with self.lock:
            if self.is_on_call:
                return
            self.is_on_call = True
            self.running = True

        full_url = f"ws://{self.server_ip}:8080{audio_ws_url}"
        logger.info(f"Подключаюсь к аудио-каналу: {full_url}")

        self.pyaudio_instance = pyaudio.PyAudio()
        # Буфер 640 байт (40 мс)
        self.audio_input_stream = self.pyaudio_instance.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=640
        )
        self.audio_output_stream = self.pyaudio_instance.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            output=True,
            frames_per_buffer=640
        )

        self.call_ws = websocket.WebSocketApp(
            full_url,
            on_open=self._on_audio_open,
            on_message=self._on_audio_message,
            on_error=self._on_audio_error,
            on_close=self._on_audio_close
        )
        wst = threading.Thread(target=self.call_ws.run_forever, daemon=True)
        wst.start()

    def _on_audio_open(self, ws):
        logger.info("🎤 Аудио-канал открыт")
        auth_msg = {
            "type": "auth_audio",
            "device_id": self.my_device_id,
            "token": self.my_token
        }
        ws.send(json.dumps(auth_msg))
        logger.info("Аутентификация аудиоканала отправлена")
        if self.send_audio:
            self.send_thread = threading.Thread(target=self._send_audio_loop, daemon=True)
            self.send_thread.start()
        else:
            logger.info("Отправка аудио отключена (только приём)")

    def _send_audio_loop(self):
        while self.running and self.is_on_call and self.call_ws and self.call_ws.sock:
            try:
                if self.audio_input_stream is None:
                    break
                data = self.audio_input_stream.read(640)
                if self.call_ws and self.call_ws.sock:
                    self.call_ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                logger.error(f"Ошибка отправки аудио: {e}")
                break
            # Без time.sleep – read сам блокируется на 40 мс
        logger.info("Поток отправки аудио завершён")

    def _on_audio_message(self, ws, message):
        if isinstance(message, bytes):
            with self.buffer_lock:
                self.packet_buffer.append(message)
                # Накопим 2 пакета (80 мс) и выведем
                if len(self.packet_buffer) >= 2:
                    combined = b''.join(self.packet_buffer)
                    self.packet_buffer.clear()
                    try:
                        if self.audio_output_stream:
                            self.audio_output_stream.write(combined)
                    except Exception as e:
                        logger.error(f"Ошибка воспроизведения: {e}")

    def _on_audio_error(self, ws, error):
        logger.error(f"Ошибка аудио-WebSocket: {error}")
        self.end_call()

    def _on_audio_close(self, ws, close_status_code, close_msg):
        logger.info("Аудио-канал закрыт")
        self.end_call()

    def end_call(self):
        with self.lock:
            if not self.is_on_call:
                return
            self.running = False
            self.is_on_call = False
            logger.info("Завершаем звонок")

        # Выводим остатки буфера
        with self.buffer_lock:
            if self.packet_buffer:
                try:
                    if self.audio_output_stream:
                        self.audio_output_stream.write(b''.join(self.packet_buffer))
                except Exception as e:
                    logger.error(f"Ошибка при финальной записи: {e}")
                self.packet_buffer.clear()

        if self.audio_input_stream:
            try:
                self.audio_input_stream.stop_stream()
                self.audio_input_stream.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия input stream: {e}")
            self.audio_input_stream = None
        if self.audio_output_stream:
            try:
                self.audio_output_stream.stop_stream()
                self.audio_output_stream.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия output stream: {e}")
            self.audio_output_stream = None
        if self.pyaudio_instance:
            try:
                self.pyaudio_instance.terminate()
            except Exception as e:
                logger.error(f"Ошибка завершения PyAudio: {e}")
            self.pyaudio_instance = None
        if self.call_ws:
            try:
                self.call_ws.close()
            except Exception as e:
                logger.error(f"Ошибка закрытия WebSocket: {e}")
            self.call_ws = None

        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=1.0)

        if self.on_call_ended:
            try:
                self.on_call_ended()
            except Exception as e:
                logger.error(f"Ошибка в on_call_ended: {e}")