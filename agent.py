import json
import os
import queue
import re
import threading
import subprocess
import sys
import time
from pathlib import Path
import difflib
import psutil
import sounddevice as sd
import vosk
import pyttsx3
from llama_cpp import Llama
import datetime
from dataclasses import dataclass
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote, quote_plus
from typing import Optional
import shlex
from lang_ru import TIME_UNITS, NUM_WORDS

CONFIG_PATH = Path(__file__).with_name("config.json")
if not CONFIG_PATH.exists():
    print("[ERROR] Файл config.json не найден.")
    sys.exit(1)

with CONFIG_PATH.open(encoding="utf-8") as f:
    cfg = json.load(f)

# Функция проверки активационного слова с учётом возможных искажений
def _is_activation(fragment: str) -> bool:
    target = cfg["activation_word"].lower()
    for word in fragment.split():
        if difflib.SequenceMatcher(None, word, target).ratio() >= 0.8:
            return True
    return False

def _remove_activation_words(text: str) -> str:
    target = cfg["activation_word"].lower()
    tokens = text.split()
    kept = []
    for t in tokens:
        if difflib.SequenceMatcher(None, t, target).ratio() >= 0.8:
            continue
        kept.append(t)
    return " ".join(kept).strip()

print("[INFO] Загрузка модели Gemma...")
llama_kwargs = {
    "model_path": cfg["model"]["path"],
    "n_ctx": cfg["model"]["ctx_size"],
}

try:
    llm = Llama(**llama_kwargs)
except Exception as e:
    print(f"[ERROR] Не удалось загрузить модель: {e}")
    sys.exit(1)
print("[INFO] Модель загружена")

_tts_queue: "queue.Queue[dict]" = queue.Queue()
_tts_thread: Optional[threading.Thread] = None

def _tts_worker():
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        voice_index = cfg["tts"]["voice_index"]
        if 0 <= voice_index < len(voices):
            engine.setProperty('voice', voices[voice_index].id)
        engine.setProperty('rate', cfg["tts"]["rate"])
        engine.setProperty('volume', cfg["tts"]["volume"])

        # Непрерывный цикл обработки без повторного запуска run loop
        engine.startLoop(False)
        while True:
            # Обрабатываем команды из очереди
            try:
                cmd = _tts_queue.get_nowait()
            except queue.Empty:
                cmd = None

            if cmd is not None:
                action = cmd.get('cmd')
                if action == 'say':
                    text = cmd.get('text', '')
                    if text:
                        engine.say(text)
                elif action == 'stop':
                    try:
                        engine.stop()
                    except Exception:
                        pass
                elif action == 'quit':
                    try:
                        engine.endLoop()
                    except Exception:
                        pass
                    break

            # Один тик цикла движка
            try:
                engine.iterate()
            except Exception as e:
                print(f"[TTS] Ошибка в цикле: {e}")
            time.sleep(0.01)
    except Exception as e:
        print(f"[TTS] Критическая ошибка TTS потока: {e}")

# Запускаем фоновый поток TTS один раз
_tts_thread = threading.Thread(target=_tts_worker, daemon=True)
_tts_thread.start()

def speak(text: str):
    try:
        while True:
            _tts_queue.get_nowait()
    except queue.Empty:
        pass

    _tts_queue.put({'cmd': 'stop'})
    _tts_queue.put({'cmd': 'say', 'text': text})
    return _tts_thread

def interrupt_speech():
    _tts_queue.put({'cmd': 'stop'})

print("Загрузка модели Vosk...")
try:
    vosk_model = vosk.Model("vosk-model-small-ru-0.22")
except Exception as e:
    print(f"Ошибка загрузки модели Vosk: {e}")
    sys.exit(1)
samplerate = 16000
rec = vosk.KaldiRecognizer(vosk_model, samplerate)

q = queue.Queue()

def audio_callback(indata, frames, time_, status):
    if status:
        print(status, file=sys.stderr)
    q.put(bytes(indata))

def kill_process(name: str) -> bool:
    found = False
    for proc in psutil.process_iter(['name']):
        try:
            proc_name = (proc.info.get('name') or '')
            if name.lower() in proc_name.lower():
                proc.kill()
                found = True
        except Exception:
            pass
    return found

COMMANDS_CFG = cfg["commands"]

# Настройки веб-поиска
_WEB_CFG = cfg["web_search"]
WEB_MAX_SOURCES = int(_WEB_CFG["max_sources"])
WEB_SEARCH_TIMEOUT = float(_WEB_CFG["search_timeout_sec"])
WEB_PAGE_TIMEOUT = float(_WEB_CFG["page_timeout_sec"])
WEB_PER_PAGE_LIMIT = int(_WEB_CFG["per_page_limit"])

# Вспомогательные функции для конфигурации
def _current_username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or Path.home().name


def _expand_config_placeholders(s: str) -> str:
    return s.replace("${USER}", _current_username())

# Выполнение команды из конфига
def execute_predefined_command(text: str) -> Optional[str]:
    lowered = text.lower()
    for key, meta in COMMANDS_CFG.items():
        if key in lowered:
            if 'запусти' in lowered or 'открой' in lowered:
                cmd = meta['open']
                try:
                    if isinstance(cmd, (list, tuple)):
                        subprocess.Popen(list(cmd))
                    else:
                        s = _expand_config_placeholders(str(cmd))
                        low = s.lower()
                        launched = False
                        for ext in (".exe", ".bat", ".cmd", ".com", ".ps1"):
                            idx = low.find(ext)
                            if idx != -1:
                                exe_path = s[: idx + len(ext)].strip().strip('"')
                                tail = s[idx + len(ext):].strip()
                                args = shlex.split(tail, posix=False) if tail else []
                                subprocess.Popen([exe_path, *args])
                                launched = True
                                break
                        if not launched:
                            raise ValueError("Команда запуска должна указывать путь к .exe/.bat/.cmd/.com/.ps1")
                    return f"Запускаю {key}."
                except Exception as e:
                    return f"Ошибка запуска {key}: {e}"
            elif 'закрой' in lowered or 'выключи' in lowered:
                target = meta['close']
                success = kill_process(target)
                return f"Закрываю {key}." if success else f"{key.capitalize()} не запущен."
    return None

# Маршрутизация команд
def route_command(text: str) -> str:

    handlers = [
        execute_predefined_command,
        execute_power_command,
        execute_volume_command,
        execute_time_command,
        execute_reminder_command,
        execute_web_search_command,
    ]
    for h in handlers:
        try:
            res = h(text)
        except Exception as e:
            print(f"[ERROR] {h.__name__}: {e}")
            res = None
        if res is not None:
            return res
    return ask_llm(text)

# Команда перезагрузки компьютера
def execute_power_command(text: str) -> Optional[str]:
    lowered = text.lower()
    if "перезагрузить компьютер" in lowered or "перезагрузи компьютер" in lowered:
        subprocess.Popen("shutdown /r /t 0", shell=True)
        return "Перезагружаю компьютер."
    if "выключи компьютер" in lowered:
        subprocess.Popen("shutdown /s /t 0", shell=True)
        return "Выключаю компьютер."
    if "спящий режим" in lowered:
        subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return "Перевожу компьютер в спящий режим."
    return None

# Команда установки громкости(1-10%,2-20%...)
def _set_master_volume(level: float):
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level, None)
    except Exception as e:
        print(f"[WARN] Не удалось изменить громкость: {e}")


def execute_volume_command(text: str) -> Optional[str]:
    cleaned = _replace_number_words(text.lower())
    m_pct = re.search(r"громкост[ьи]\s+(\d+)\s*(%|процент\w*)", cleaned, re.IGNORECASE)
    if m_pct:
        pct = int(m_pct.group(1))
        pct = max(0, min(pct, 100))
        _set_master_volume(pct / 100)
        return f"Громкость установлена на {pct}%."
    m = re.search(r"громкост[ьи]\s+(\d+)", cleaned, re.IGNORECASE)
    if m:
        level_num = int(m.group(1))
        level_num = max(0, min(level_num, 10))
        _set_master_volume(level_num / 10)
        return f"Громкость установлена на {level_num * 10}%."
    return None

def execute_time_command(text: str) -> Optional[str]:
    lowered = text.lower().strip()
    if (
        re.search(r"\bсколько\s+времени?\b", lowered)
        or re.search(r"\bкакое\s+время\b", lowered)
        or re.search(r"\bсколько\s+время\b", lowered)
        or re.search(r"\bкоторый\s+час\b", lowered)
        or re.fullmatch(r"время", lowered)
    ):
        now = datetime.datetime.now()
        return f"Сейчас {now.strftime('%H:%M')}."
    return None

# Напоминания и таймеры
@dataclass
class _Reminder:
    ts: float
    message: str

_scheduled: list[_Reminder] = []


def _scheduler():
    while True:
        now = time.time()
        for task in _scheduled[:]:
            if now >= task.ts:
                speak(task.message)
                print(f"[REMINDER] {task.message}")
                _scheduled.remove(task)
        time.sleep(1)


threading.Thread(target=_scheduler, daemon=True).start()

# Удаление напоминаний
def execute_reminder_command(text: str) -> Optional[str]:
    lowered = text.lower()
    cleaned = _replace_number_words(lowered)
    m = re.search(r"(удали(?:ть)?|отмени(?:ть)?)\s+напоминани[ея]\s+на\s+(?P<h>\d{1,2})(?::|\.|\s)(?:(?P<m1>\d)\s+(?P<m2>\d)|(?P<m>\d{1,2}))", cleaned)
    if m:
        hour = int(m.group('h'))
        if m.group('m') is not None:
            minute = int(m.group('m'))
        else:
            minute = int((m.group('m1') or '0') + (m.group('m2') or '0'))
        hour = max(0, min(hour, 23))
        minute = max(0, min(minute, 59))
        target_str = f"{hour:02d}:{minute:02d}"
        removed = 0
        for task in list(_scheduled):
            try:
                dt = datetime.datetime.fromtimestamp(task.ts)
                if dt.strftime("%H:%M") == target_str:
                    _scheduled.remove(task)
                    removed += 1
            except Exception:
                continue
        return (f"Удалено напоминание на {target_str}" if removed
                else f"Напоминаний на {target_str} не найдено.")

    # Таймер
    m = re.search(r"таймер\s+(\d+)\s+([а-яa-z]+)", cleaned)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        sec = n * TIME_UNITS.get(unit, 60)
        _scheduled.append(_Reminder(time.time() + sec, f"Таймер {n} {unit} завершён."))
        return f"Таймер на {n} {unit} установлен."

    # Напоминание
    m = re.search(r"напомни(?:ть)?\s+через\s+(\d+)\s+([а-яa-z]+)\s+(.+)", cleaned)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        message = m.group(3).strip()
        sec = n * TIME_UNITS.get(unit, 60)
        _scheduled.append(_Reminder(time.time() + sec, message))
        return f"Напоминание через {n} {unit} установлено."

    # Напоминание на конкретное время
    m = re.search(r"напоминани[ея]\s+на\s+(?P<h>\d{1,2})(?::|\.|\s)(?:(?P<m1>\d)\s+(?P<m2>\d)|(?P<m>\d{1,2}))(?:\s+(?P<msg>.+))?$", cleaned)
    if m:
        hour = int(m.group('h'))
        if m.group('m') is not None:
            minute = int(m.group('m'))
        else:
            minute = int((m.group('m1') or '0') + (m.group('m2') or '0'))
        message = (m.group('msg') or "").strip()
        if not message or message == "0":
            message = "Напоминание"
        # Нормализация границ времени
        hour = max(0, min(hour, 23))
        minute = max(0, min(minute, 59))
        now_dt = datetime.datetime.now()
        target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_dt:
            target += datetime.timedelta(days=1)
        _scheduled.append(_Reminder(target.timestamp(), message))
        return f"Напоминание на {target.strftime('%H:%M')} установлено."
    return None

# Работа с LLM
SYSTEM_PROMPT = """Ты — русскоязычный голосовой помощник по имени Гемма, который всегда отвечает кратко, чётко и структурированно. Отвечай в формате, где информация разбита на логические блоки. Избегай длинных текстов, эмоциональных описаний и избыточной детализации. Основная цель — передать информацию максимально эффективно. """

def ask_llm(user_text: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": user_text})
    allowed = {"temperature", "top_p", "top_k", "repeat_penalty", "max_tokens", "seed", "stop"}
    mcfg = cfg["model"]
    gen_args = {k: mcfg[k] for k in allowed if k in mcfg}
    result = llm.create_chat_completion(messages=messages, **gen_args)
    assistant_reply = result["choices"][0]["message"]["content"].strip()
    return assistant_reply

# Поиск в интернете (web_search)
def execute_web_search_command(text: str) -> Optional[str]:
    m = re.match(r"^\s*найди\s+(.+)", text, flags=re.IGNORECASE)
    if not m:
        return None
    query = m.group(1).strip()
    if not query:
        return "Что искать? Скажите: 'найди' и запрос."
    try:
        return web_search_answer(query)
    except Exception as e:
        print(f"[WEB_SEARCH] Ошибка: {e}")
        return "Не удалось выполнить поиск сейчас. Попробуйте позже."


def _extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    parts = []
    for t in root.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = t.get_text(" ", strip=True)
        if txt:
            parts.append(txt)
    text = "\n".join(parts)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_search_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if not href:
            continue
        if "uddg=" in href:
            try:
                if "?" in href:
                    qs = parse_qs(href.split("?", 1)[1])
                    if "uddg" in qs and qs["uddg"]:
                        url = unquote(qs["uddg"][0])
                        if url.lower().startswith("http"):
                            links.append(url)
                            continue
            except Exception:
                pass
        if href.lower().startswith("http") and "duckduckgo.com" not in href.lower():
            links.append(href)
    return links


def web_search_answer(query: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    search_urls = [
        f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}&kl=ru-ru",
    ]

    links: list[str] = []
    for s_url in search_urls:
        try:
            resp = requests.get(s_url, headers=headers, timeout=WEB_SEARCH_TIMEOUT)
            resp.raise_for_status()
            links.extend(_extract_search_links(resp.text))
            if len(links) >= WEB_MAX_SOURCES * 5:
                break
        except Exception as e:
            print(f"[WEB_SEARCH] Поиск не удался на {s_url}: {e}")
    try:
        dbg_domains = []
        for l in links[:5]:
            try:
                dbg_domains.append(urlparse(l).netloc)
            except Exception:
                pass
        print(f"[WEB_SEARCH] Найдено кандидатов: {len(links)}; примеры доменов: {dbg_domains}")
    except Exception:
        pass

    if not links:
        return "Не нашла подходящих результатов. Сформулируйте запрос иначе."

    # Поочерёдно обходим кандидатов и берём первые пригодные источники
    seen_domains = set()
    context_chunks: list[str] = []
    used_domains: list[str] = []
    used_urls: list[str] = []
    per_page_limit = WEB_PER_PAGE_LIMIT
    total_limit = max(per_page_limit, per_page_limit * max(1, WEB_MAX_SOURCES))
    for href in links:
        if not href:
            continue
        url = href if href.lower().startswith("http") else None
        if not url:
            continue
        domain = urlparse(url).netloc.lower()
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        try:
            r = requests.get(url, headers=headers, timeout=WEB_PAGE_TIMEOUT)
            ctype = r.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype:
                continue
            text = _extract_visible_text(r.text)
            if not text:
                continue
            text = text[:per_page_limit]
            chunk = f"[Источник: {domain} | {url}] {text}"
            context_chunks.append(chunk)
            used_domains.append(domain)
            used_urls.append(url)
            if (sum(len(x) for x in context_chunks) >= total_limit) or (len(used_domains) >= WEB_MAX_SOURCES):
                break
        except Exception:
            continue

    if used_domains:
        try:
            print(f"[WEB_SEARCH] Выбраны источники: {', '.join(used_urls)}")
        except Exception:
            pass

    if not context_chunks:
        return "Не удалось извлечь текст из результатов поиска. Попробуйте другой запрос."

    context_text = "\n\n".join(context_chunks)
    system_extra = (
        "Ты отвечаешь кратко на русском, основываясь ТОЛЬКО на контексте ниже. Запрещено добавлять факты вне контекста. Если информации недостаточно, явно скажи об этом."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + " " + system_extra},
        {"role": "user", "content": (
            f"Вопрос: {query}\n\n"
            f"Контекст из источников:\n{context_text}\n\n"
            "Сформулируй краткий ответ (2–4 предложения)."
        )},
    ]
    try:
        allowed = {"temperature", "top_p", "top_k", "repeat_penalty", "max_tokens", "seed", "stop"}
        mcfg = cfg["model"]
        gen_args = {k: mcfg[k] for k in allowed if k in mcfg}
        result = llm.create_chat_completion(messages=messages, **gen_args)
        assistant_reply = result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[WEB_SEARCH] Ошибка генерации ответа: {e}")
        assistant_reply = "Не удалось сгенерировать ответ."

    # перечисление источников (полные ссылки)
    sources_tail = f" (источники: {', '.join(used_urls)})" if used_urls else ""
    return assistant_reply + sources_tail

# Парсит числительные в число 0-99, возвращает (value, used_tokens)
def _parse_number(tokens: list[str]) -> Optional[tuple[int, int]]:
    if not tokens:
        return None
    t0 = tokens[0]
    if t0 not in NUM_WORDS:
        return None
    first_val = NUM_WORDS[t0]
    used = 1
    if len(tokens) >= 2 and tokens[1] in NUM_WORDS:
        second_val = NUM_WORDS[tokens[1]]
        # десятки + единицы: "тридцать пять"
        if first_val >= 20 and first_val % 10 == 0 and second_val < 10:
            return (first_val + second_val, 2)
        # ноль + единица: "ноль восемь", "ноль ноль"
        if first_val == 0 and 0 <= second_val < 10:
            return (second_val, 2)
    return (first_val, used)

def _words_to_number(tokens: list[str]) -> Optional[int]:
    parsed = _parse_number(tokens)
    return parsed[0] if parsed is not None else None


def _replace_number_words(text: str) -> str:
    tokens = text.split()
    result = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in NUM_WORDS:
            # собираем до 2 токенов максимум (десятки + единицы)
            lookahead = tokens[i:i + 2]
            parsed = _parse_number(lookahead)
            if parsed is not None:
                num, used = parsed
                result.append(str(num))
                i += used
                continue
        result.append(token)
        i += 1
    return " ".join(result)

print("[INFO] Система готова. Скажите ключевое слово.")
silence_timeout = cfg["silence_timeout"]

with sd.RawInputStream(samplerate=samplerate, blocksize=8000, dtype='int16', channels=1, callback=audio_callback):
    last_audio_time = time.time()
    listening_for_command = False
    command_buffer = []
    while True:
        data = q.get()
        if rec.AcceptWaveform(data):
            result = rec.Result()
            text = json.loads(result)["text"].lower().strip()
            if text:
                print(f"[VOSK] {text}")
            if not text:
                continue

            # Прерываем речь ТОЛЬКО если сказано ключевое слово (активация)
            if _is_activation(text):
                interrupt_speech()

            if not listening_for_command:
                if _is_activation(text):
                    command_text = _remove_activation_words(text)
                    if command_text:
                        user_command = command_text
                    else:
                        speak("Я слушаю. Какую команду выполнить?")
                        listening_for_command = True
                        last_audio_time = time.time()
                        command_buffer.clear()
                        continue
                else:
                    # Игнорируем речь без ключевого слова
                    continue
            else:
                user_command = text
                listening_for_command = False

            response = route_command(user_command)
            print(f"[ASSISTANT] {response}")
            speak(response)
        else:
            # анализируем промежуточный результат, чтобы ловить ключевое слово без задержки
            partial = json.loads(rec.PartialResult()).get("partial", "").lower().strip()
            if partial:
                # вывод промежуточного результата
                print(f"[VOSK:partial] {partial}")
                # Пока пользователь говорит — обновляем таймер тишины
                if listening_for_command:
                    last_audio_time = time.time()

            # проверяем тайм-аут тишины
            if listening_for_command and (time.time() - last_audio_time > silence_timeout):
                listening_for_command = False