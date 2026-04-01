# app.py (финальная версия с интеграцией сайта)
import os
import logging
import json
import requests
import sqlite3
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

# ================== КОНФИГУРАЦИЯ ==================
FOLDER_ID = os.environ.get('FOLDER_ID')
API_KEY = os.environ.get('API_KEY')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.environ.get('ADMIN_IDS', '').split(',') if id.strip()]

# ================== ИНИЦИАЛИЗАЦИЯ ==================
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ================== БАЗА ДАННЫХ (SQLITE) ==================
def get_db_connection():
    return sqlite3.connect('studyhelper.db')

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                subscription_type TEXT DEFAULT 'free',
                subscription_end DATE,
                requests_remaining INTEGER DEFAULT 5,
                last_request_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                query_text TEXT,
                response_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                payment_id TEXT,
                plan_type TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        logging.info("Database initialized")

init_db()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def get_user_info(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT subscription_type, subscription_end, requests_remaining, last_request_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "type": row[0],
            "end_date": row[1],
            "remaining": row[2],
            "last_date": row[3]
        }

def update_user_subscription(user_id: int, plan_type: str, days: int, requests_limit: int):
    end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users 
            SET subscription_type = ?, subscription_end = ?, requests_remaining = ? 
            WHERE user_id = ?
        """, (plan_type, end_date, requests_limit, user_id))
        conn.commit()

def refresh_free_requests(user_id: int):
    today = datetime.now().date().isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_request_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0] != today:
            cursor.execute("UPDATE users SET requests_remaining = 5, last_request_date = ? WHERE user_id = ?", (today, user_id))
            conn.commit()
            return 5
        elif not row:
            cursor.execute("INSERT INTO users (user_id, requests_remaining, last_request_date) VALUES (?, 5, ?)", (user_id, today))
            conn.commit()
            return 5
        return None

def can_make_request(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True

    user = get_user_info(user_id)
    if not user:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (user_id, requests_remaining, last_request_date) VALUES (?, 5, ?)", 
                           (user_id, datetime.now().date().isoformat()))
            conn.commit()
        return True

    if user["type"] != "free":
        if user["end_date"] and user["end_date"] >= datetime.now().date().isoformat():
            return user["remaining"] > 0
        else:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET subscription_type = 'free', subscription_end = NULL, requests_remaining = 5, last_request_date = ? WHERE user_id = ?",
                               (datetime.now().date().isoformat(), user_id))
                conn.commit()
            return True

    refresh_free_requests(user_id)
    user = get_user_info(user_id)
    return user["remaining"] > 0

def decrement_request(user_id: int):
    if user_id in ADMIN_IDS:
        return
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET requests_remaining = requests_remaining - 1 WHERE user_id = ?", (user_id,))
        conn.commit()

def save_query(user_id: int, query: str, response: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO queries (user_id, query_text, response_text) VALUES (?, ?, ?)",
                       (user_id, query[:500], response[:500]))
        conn.commit()

def send_telegram_message(chat_id: int, text: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

def send_telegram_photo(chat_id: int, photo_data: bytes, caption: str = ""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    files = {"photo": photo_data}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
    requests.post(url, data=data, files=files)

def send_main_keyboard(chat_id: int, text: str = "📋 Главное меню"):
    keyboard = {
        "keyboard": [
            ["📝 Пересказать текст", "📝 Создать тест"],
            ["🔍 Объяснить понятие", "✍️ Написать эссе"],
            ["🔢 Реши задачу", "📷 Распознать текст"],
            ["🎤 Распознать голос", "⭐ Премиум"],
            ["🎁 Рефералка"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    send_telegram_message(chat_id, text, json.dumps(keyboard))

def remove_keyboard(chat_id: int, text: str):
    markup = {"remove_keyboard": True}
    send_telegram_message(chat_id, text, json.dumps(markup))

# ================== YANDEXGPT (languageModels) ==================
def call_yandexgpt(system_prompt: str, user_message: str) -> str:
    if len(user_message) > 3000:
        user_message = user_message[:3000] + "…"
    prompt = {
        "modelUri": f"gpt://{FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.6, "maxTokens": 2000},
        "messages": [
            {"role": "system", "text": system_prompt[:1000]},
            {"role": "user", "text": user_message}
        ]
    }
    try:
        resp = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {API_KEY}", "Content-Type": "application/json"},
            json=prompt,
            timeout=30
        )
        logging.info(f"YandexGPT status: {resp.status_code}, body: {resp.text[:500]}")
        if resp.status_code == 200:
            return resp.json()['result']['alternatives'][0]['message']['text']
        else:
            return f"⚠️ Ошибка YandexGPT: {resp.status_code} - {resp.text}"
    except Exception as e:
        logging.error(f"YandexGPT error: {e}")
        return "⚠️ Не удалось получить ответ. Попробуйте позже."

def summarize_text(text: str) -> str:
    system = "Ты — ассистент. Сделай краткий пересказ текста, выдели главные мысли."
    return call_yandexgpt(system, f"Перескажи текст:\n{text[:3000]}")

def generate_test(topic: str) -> str:
    system = "Ты — создатель тестов. Создай тест из 5 вопросов с вариантами ответов и укажи правильный."
    return call_yandexgpt(system, f"Создай тест по теме: {topic}")

def explain_concept(concept: str) -> str:
    system = "Ты — преподаватель. Объясни понятие простым языком с примерами."
    return call_yandexgpt(system, f"Объясни понятие: {concept}")

def generate_essay(topic: str) -> str:
    system = "Ты — писатель. Напиши небольшое эссе на заданную тему, структурированно и интересно."
    return call_yandexgpt(system, f"Напиши эссе на тему: {topic}")

def solve_task(problem: str) -> str:
    system = "Ты — эксперт по решению задач. Помоги пользователю решить задачу шаг за шагом."
    return call_yandexgpt(system, f"Реши задачу:\n{problem}")

# ================== YANDEX VISION (распознавание текста на фото) ==================
def recognize_image(file_content: bytes) -> str:
    url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
    headers = {
        "Authorization": f"Api-Key {API_KEY}",
        "Content-Type": "application/json"
    }
    img_base64 = base64.b64encode(file_content).decode('utf-8')
    body = {
        "folderId": FOLDER_ID,
        "analyze_specs": [{
            "content": img_base64,
            "features": [{
                "type": "TEXT_DETECTION",
                "text_detection_config": {"language_codes": ["ru", "en"]}
            }]
        }]
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        logging.info(f"Vision status: {resp.status_code}, body: {resp.text[:500]}")
        if resp.status_code == 200:
            data = resp.json()
            text_blocks = []
            for result in data.get("results", []):
                for text_annotation in result.get("textDetection", []):
                    text_blocks.append(text_annotation.get("text", ""))
            if text_blocks:
                return "\n".join(text_blocks)
            else:
                return "На изображении не найден текст."
        else:
            return f"Ошибка Vision API: {resp.status_code} - {resp.text}"
    except Exception as e:
        logging.error(f"Vision error: {e}")
        return "⚠️ Не удалось распознать изображение."

# ================== YANDEX SPEECHKIT STT (распознавание речи) ==================
def recognize_speech(file_content: bytes) -> str:
    url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
    headers = {
        "Authorization": f"Api-Key {API_KEY}"
    }
    params = {
        "folderId": FOLDER_ID,
        "lang": "ru-RU",
        "format": "oggopus"
    }
    try:
        resp = requests.post(url, headers=headers, params=params, data=file_content, timeout=30)
        logging.info(f"STT status: {resp.status_code}, body: {resp.text[:500]}")
        if resp.status_code == 200:
            result = resp.json()
            return result.get("result", "Не удалось распознать речь")
        elif resp.status_code == 401:
            return "⚠️ Ошибка авторизации SpeechKit. Убедитесь, что сервисный аккаунт имеет роль `ai.speechkit-stt.user`."
        else:
            return f"Ошибка STT: {resp.status_code} - {resp.text}"
    except Exception as e:
        logging.error(f"STT error: {e}")
        return "⚠️ Не удалось распознать голос."

# ================== ОБРАБОТЧИКИ TELEGRAM ==================
user_states = {}

def handle_telegram_update(update):
    if "message" not in update:
        return

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()
    first_name = msg["from"].get("first_name", "")
    username = msg["from"].get("username", "")

    # Регистрация
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                       (user_id, username, first_name))
        conn.commit()

    # Состояние
    state = user_states.get(user_id)
    if state:
        handle_state_input(user_id, chat_id, text, state)
        return

    # Команды
    if text == "/start":
        user = get_user_info(user_id)
        if user_id in ADMIN_IDS:
            premium_status = "✅ Бессрочно (админ)"
        elif user and user["type"] != "free":
            premium_status = f"✅ {user['type'].upper()} до {user['end_date']} (осталось {user['remaining']} запросов)"
        else:
            premium_status = "❌ Неактивна"
        send_main_keyboard(chat_id,
                           f"🎓 Привет, {first_name}!\n\n"
                           f"Я StudyHelperBot — твой AI-ассистент для учёбы.\n\n"
                           f"📊 Статус: Премиум {premium_status}\n"
                           f"🔓 Бесплатно: 5 запросов/день (админу безлимит)\n\n"
                           f"Выбери действие:")
        return

    elif text == "/help":
        send_telegram_message(chat_id,
                              "📖 **Доступные команды:**\n"
                              "/start — Главное меню\n"
                              "/menu — Показать главное меню\n"
                              "/premium — Подписка\n"
                              "/help — Эта справка\n\n"
                              "**Как пользоваться:**\n"
                              "1. Нажми на кнопку внизу\n"
                              "2. Введи текст, загрузи фото или отправь голосовое\n"
                              "3. Получи ответ от AI")
        return

    elif text == "/menu" or text == "Меню" or text == "меню":
        send_main_keyboard(chat_id, "📋 Главное меню:")
        return

    elif text == "/premium":
        kb = {
            "inline_keyboard": [
                [{"text": "💎 Премиум (250 запросов/мес) — 150 руб", "callback_data": "buy_premium"}],
                [{"text": "💎 Премиум+ (500 запросов/мес) — 300 руб", "callback_data": "buy_premium_plus"}],
                [{"text": "🎁 Реферальная программа", "callback_data": "referral"}]
            ]
        }
        send_telegram_message(chat_id,
                              "🌟 **Выберите тариф подписки:**\n\n"
                              "💎 **Премиум** – 250 запросов/мес, 150 руб\n"
                              "💎 **Премиум+** – 500 запросов/мес, 300 руб\n\n"
                              "Оплата производится через платёжную систему.",
                              json.dumps(kb))
        return

    # Обработка кнопок обычной клавиатуры
    if text in ["📝 Пересказать текст", "📝 Создать тест", "🔍 Объяснить понятие",
                "✍️ Написать эссе", "🔢 Реши задачу", "📷 Распознать текст"]:
        if not can_make_request(user_id):
            send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. /premium")
            return
        mapping = {
            "📝 Пересказать текст": "summarize",
            "📝 Создать тест": "test",
            "🔍 Объяснить понятие": "explain",
            "✍️ Написать эссе": "essay",
            "🔢 Реши задачу": "solve_task",
            "📷 Распознать текст": "recognize_image"
        }
        state = mapping[text]
        prompts = {
            "summarize": "📄 Отправь текст для пересказа.",
            "test": "📝 Напиши тему теста.",
            "explain": "🔍 Напиши понятие для объяснения.",
            "essay": "✍️ Напиши тему эссе.",
            "solve_task": "🔢 Напиши условие задачи.",
            "recognize_image": "🖼 Отправьте изображение для распознавания текста."
        }
        remove_keyboard(chat_id, prompts[state])
        user_states[user_id] = state

    elif text == "🎤 Распознать голос":
        if not can_make_request(user_id):
            send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. /premium")
            return
        remove_keyboard(chat_id, "🎤 Отправьте голосовое сообщение.")
        user_states[user_id] = "recognize_voice"

    elif text == "⭐ Премиум":
        # Ссылка на сайт с параметрами
        username = msg["from"].get("username", str(user_id))
        payment_link = f"https://annually-immediate-yak.tilda.ws/?user_id={user_id}&tg_username={username}"
        kb = {"inline_keyboard": [[{"text": "💳 Перейти на сайт", "url": payment_link}]]}
        send_telegram_message(chat_id, "🌟 **Выберите тариф на сайте:**", json.dumps(kb))

    elif text == "🎁 Рефералка":
        ref_link = f"https://t.me/unistudyhelper_bot?start=ref_{user_id}"
        send_telegram_message(chat_id,
                              f"🎁 **Реферальная программа**\n\n"
                              f"Твоя ссылка:\n`{ref_link}`\n\n"
                              "Приведи друга — получи скидку 20% на следующий месяц!")
    else:
        send_telegram_message(chat_id,
                              "Пожалуйста, используй кнопки внизу или команды.\n"
                              "Если клавиатура не отображается, нажми /start")

def handle_state_input(user_id: int, chat_id: int, text: str, state: str):
    if not can_make_request(user_id):
        send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. /premium")
        del user_states[user_id]
        send_main_keyboard(chat_id, "Главное меню:")
        return

    reply = None
    if state == "summarize":
        reply = summarize_text(text)
    elif state == "test":
        reply = generate_test(text)
    elif state == "explain":
        reply = explain_concept(text)
    elif state == "essay":
        reply = generate_essay(text)
    elif state == "solve_task":
        reply = solve_task(text)
    else:
        # Медиа обрабатываются отдельно
        send_telegram_message(chat_id, "Пожалуйста, используйте кнопки для выбора действия.")
        del user_states[user_id]
        send_main_keyboard(chat_id)
        return

    if reply:
        save_query(user_id, text, reply)
        send_telegram_message(chat_id, reply)
    decrement_request(user_id)
    del user_states[user_id]
    send_main_keyboard(chat_id, "Что ещё сделать?")

def handle_media(update):
    """Обработка фото и голосовых сообщений"""
    if "message" not in update:
        return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    state = user_states.get(user_id)

    # Фото
    if state == "recognize_image" and "photo" in msg:
        if not can_make_request(user_id):
            send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. /premium")
            del user_states[user_id]
            send_main_keyboard(chat_id, "Главное меню:")
            return

        file_id = msg["photo"][-1]["file_id"]
        file_info = get_file(file_id)
        if file_info:
            file_content = download_file(file_info["file_path"])
            if file_content:
                send_telegram_message(chat_id, "🖼 Распознаю текст на изображении...")
                recognized = recognize_image(file_content)
                send_telegram_message(chat_id, f"📝 *Распознанный текст:*\n{recognized}")
                save_query(user_id, "Распознавание изображения", recognized)
                decrement_request(user_id)
            else:
                send_telegram_message(chat_id, "⚠️ Не удалось загрузить изображение.")
        else:
            send_telegram_message(chat_id, "⚠️ Не удалось получить файл.")
        del user_states[user_id]
        send_main_keyboard(chat_id, "Что ещё сделать?")

    # Голосовое
    elif state == "recognize_voice" and "voice" in msg:
        if not can_make_request(user_id):
            send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. /premium")
            del user_states[user_id]
            send_main_keyboard(chat_id, "Главное меню:")
            return

        file_id = msg["voice"]["file_id"]
        file_info = get_file(file_id)
        if file_info:
            file_content = download_file(file_info["file_path"])
            if file_content:
                send_telegram_message(chat_id, "🎤 Распознаю голосовое сообщение...")
                recognized = recognize_speech(file_content)
                if recognized and "Не удалось" not in recognized and "Ошибка" not in recognized and "авторизации" not in recognized:
                    send_telegram_message(chat_id, f"📝 *Распознанный текст:*\n{recognized}")
                    gpt_reply = call_yandexgpt("Ты полезный ассистент.", f"Ответь на голосовое сообщение: {recognized}")
                    send_telegram_message(chat_id, gpt_reply)
                    save_query(user_id, f"Голосовое: {recognized}", gpt_reply)
                else:
                    send_telegram_message(chat_id, recognized)
                    save_query(user_id, "Голосовое сообщение", recognized)
                decrement_request(user_id)
            else:
                send_telegram_message(chat_id, "⚠️ Не удалось загрузить голосовое сообщение.")
        else:
            send_telegram_message(chat_id, "⚠️ Не удалось получить файл.")
        del user_states[user_id]
        send_main_keyboard(chat_id, "Что ещё сделать?")

def get_file(file_id: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    resp = requests.get(url, params={"file_id": file_id})
    if resp.status_code == 200:
        data = resp.json()
        if data["ok"]:
            return data["result"]
    return None

def download_file(file_path: str):
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.content
    return None

# ================== CALLBACK-ОБРАБОТЧИКИ ==================
def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    user_id = callback["from"]["id"]
    data = callback["data"]
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                  json={"callback_query_id": callback["id"]})

    if data == "buy_premium":
        plan = "premium"
        amount = 150
        requests_limit = 250
    elif data == "buy_premium_plus":
        plan = "premium_plus"
        amount = 300
        requests_limit = 500
    elif data == "referral":
        ref_link = f"https://t.me/unistudyhelper_bot?start=ref_{user_id}"
        send_telegram_message(chat_id,
                              f"🎁 **Реферальная программа**\n\n"
                              f"Твоя ссылка:\n`{ref_link}`\n\n"
                              "Приведи друга — получи скидку 20% на следующий месяц!")
        return
    else:
        return

    # Получаем username пользователя для передачи на сайт
    username = callback["from"].get("username", str(user_id))
    # Ссылка на сайт с параметрами
    payment_link = f"https://annually-immediate-yak.tilda.ws/?user_id={user_id}&tg_username={username}&plan={plan}"
    kb = {"inline_keyboard": [[{"text": f"💳 Оплатить {amount} руб", "url": payment_link}]]}
    send_telegram_message(chat_id,
                          f"💳 **Оформление подписки {plan.upper()}**\n\n"
                          f"Стоимость: **{amount} руб/мес**\n"
                          f"Количество запросов: **{requests_limit}** в месяц\n\n"
                          "🔗 Нажми на кнопку ниже, чтобы перейти к оплате на сайте.\n"
                          "После успешной оплаты подписка активируется автоматически.",
                          json.dumps(kb))

# ================== ВЕБХУК ==================
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logging.info(f"Received update: {update}")
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    else:
        # Сначала обрабатываем медиа (фото/голос)
        handle_media(update)
        # Затем текстовые сообщения (кнопки, команды)
        handle_telegram_update(update)
    return "OK", 200

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_msg = data.get('message', '').strip()
    if not user_msg:
        return jsonify({"reply": "Напишите что-нибудь"}), 400
    reply = call_yandexgpt("Ты полезный ассистент", user_msg)
    return jsonify({"reply": reply})

@app.route('/payment-webhook', methods=['POST'])
def payment_webhook():
    data = request.get_json()
    user_id = data.get('user_id')
    payment_id = data.get('payment_id')
    amount = data.get('amount')
    plan = data.get('plan')
    status = data.get('status')

    if status == 'paid' and user_id:
        if plan == "premium":
            days = 30
            requests_limit = 250
            plan_type = "premium"
        elif plan == "premium_plus":
            days = 30
            requests_limit = 500
            plan_type = "premium_plus"
        else:
            return jsonify({"status": "error", "message": "Invalid plan"}), 400

        end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET subscription_type = ?, subscription_end = ?, requests_remaining = ? WHERE user_id = ?",
                           (plan_type, end_date, requests_limit, user_id))
            cursor.execute("INSERT INTO payments (user_id, amount, payment_id, plan_type, status) VALUES (?, ?, ?, ?, ?)",
                           (user_id, amount, payment_id, plan_type, status))
            conn.commit()
        send_telegram_message(user_id, f"✅ Оплата подтверждена! Подписка {plan_type.upper()} активна до {end_date}. У вас {requests_limit} запросов.")
        send_main_keyboard(user_id, "Теперь у вас премиум-доступ!")
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid payment data"}), 400

# ================== ЗАПУСК ==================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)