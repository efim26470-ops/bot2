# app.py (SQLite version with fixed callback handling)
import os
import logging
import json
import requests
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

# ================== КОНФИГУРАЦИЯ ==================
FOLDER_ID = os.environ.get('FOLDER_ID')
API_KEY = os.environ.get('API_KEY')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
# ADMIN_IDS может быть строкой с числами через запятую
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
                subscription_end DATE,
                requests_today INTEGER DEFAULT 0,
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
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        logging.info("Database initialized")

init_db()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def is_premium(user_id: int) -> bool:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        if result and result[0]:
            end_date = datetime.strptime(result[0], '%Y-%m-%d').date()
            return end_date >= datetime.now().date()
        return False

def increment_requests(user_id: int) -> int:
    today = datetime.now().date().isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT requests_today, last_request_date FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            if row[1] != today:
                cursor.execute("UPDATE users SET requests_today = 1, last_request_date = ? WHERE user_id = ?", (today, user_id))
                conn.commit()
                return 1
            else:
                cursor.execute("UPDATE users SET requests_today = requests_today + 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                cursor.execute("SELECT requests_today FROM users WHERE user_id = ?", (user_id,))
                count = cursor.fetchone()[0]
                return count
        else:
            cursor.execute("INSERT INTO users (user_id, requests_today, last_request_date) VALUES (?, 1, ?)", (user_id, today))
            conn.commit()
            return 1

def can_make_request(user_id: int) -> bool:
    if is_premium(user_id):
        return True
    return increment_requests(user_id) <= 5

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

# ================== YANDEXGPT ИНТЕГРАЦИЯ ==================
def call_yandexgpt(system_prompt: str, user_message: str) -> str:
    prompt = {
        "modelUri": f"gpt://{FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.6, "maxTokens": 2000},
        "messages": [
            {"role": "system", "text": system_prompt},
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
        if resp.status_code == 200:
            return resp.json()['result']['alternatives'][0]['message']['text']
        else:
            return f"⚠️ Ошибка YandexGPT: {resp.status_code}"
    except Exception as e:
        logging.error(f"YandexGPT error: {e}")
        return "⚠️ Не удалось получить ответ. Попробуйте позже."

def generate_conspect(text: str) -> str:
    system = "Ты — ассистент-репетитор. Сделай краткий конспект текста, структурируй, используй заголовки и списки."
    return call_yandexgpt(system, f"Сделай конспект:\n{text[:3000]}")

def generate_test(topic: str) -> str:
    system = "Ты — создатель тестов. Создай тест из 5 вопросов с вариантами ответов и укажи правильный."
    return call_yandexgpt(system, f"Создай тест по теме: {topic}")

def explain_concept(concept: str) -> str:
    system = "Ты — преподаватель. Объясни понятие простым языком с примерами."
    return call_yandexgpt(system, f"Объясни понятие: {concept}")

# ================== ОБРАБОТЧИКИ TELEGRAM ==================
def handle_telegram_update(update):
    # ** ВАЖНО: сначала обрабатываем callback-запросы (кнопки) **
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    # Если нет ни сообщения, ни callback — выходим
    if "message" not in update:
        return

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")
    first_name = msg["from"].get("first_name", "")
    username = msg["from"].get("username", "")

    # Регистрируем пользователя (если ещё нет)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                       (user_id, username, first_name))
        conn.commit()

    # Обработка команд
    if text == "/start":
        kb = {
            "inline_keyboard": [
                [{"text": "📚 Сделать конспект", "callback_data": "conspect"}],
                [{"text": "📝 Создать тест", "callback_data": "test"}],
                [{"text": "🔍 Объяснить понятие", "callback_data": "explain"}],
                [{"text": "⭐ Премиум", "callback_data": "premium"}]
            ]
        }
        premium_status = "✅ Активна" if is_premium(user_id) else "❌ Неактивна"
        send_telegram_message(chat_id,
                              f"🎓 Привет, {first_name}!\n\n"
                              f"Я StudyHelperBot — твой AI-ассистент для учёбы.\n\n"
                              f"📊 Статус: Премиум {premium_status}\n"
                              f"🔓 Бесплатно: 5 запросов/день\n\n"
                              f"Выбери действие:",
                              json.dumps(kb))
        return

    elif text == "/help":
        send_telegram_message(chat_id,
                              "📖 **Доступные команды:**\n"
                              "/start — Главное меню\n"
                              "/premium — Подписка\n"
                              "/help — Эта справка\n\n"
                              "**Как пользоваться:**\n"
                              "1. Напиши /start и выбери функцию\n"
                              "2. Загрузи текст лекции — получи конспект\n"
                              "3. Напиши тему для теста\n"
                              "4. Напиши понятие для объяснения")
        return

    elif text == "/premium":
        kb = {
            "inline_keyboard": [
                [{"text": "💳 Оформить подписку (150 руб/мес)", "callback_data": "buy_premium"}],
                [{"text": "🎁 Реферальная программа", "callback_data": "referral"}]
            ]
        }
        send_telegram_message(chat_id,
                              "🌟 **Премиум-подписка**\n\n"
                              "Всего за **150 руб/мес** ты получаешь:\n"
                              "✅ Безлимитные запросы к AI\n"
                              "✅ Приоритетную обработку\n"
                              "✅ Сохранение истории\n\n"
                              "💡 Приведи друга — получи скидку 20%!",
                              json.dumps(kb))
        return

    # Обычное текстовое сообщение (не команда)
    if not can_make_request(user_id):
        send_telegram_message(chat_id,
                              "⚠️ Ты исчерпал лимит бесплатных запросов на сегодня.\n"
                              "Оформи премиум за 150 руб/мес для безлимита!\n"
                              "/premium")
        return

    system_prompt = "Ты полезный ассистент. Отвечай кратко и по делу."
    reply = call_yandexgpt(system_prompt, text)
    save_query(user_id, text, reply)
    send_telegram_message(chat_id, reply)

def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    user_id = callback["from"]["id"]
    data = callback["data"]
    # Обязательно отвечаем на callback, чтобы кнопка перестала грузиться
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                  json={"callback_query_id": callback["id"]})

    if data == "conspect":
        send_telegram_message(chat_id, "📚 Отправь текст лекции или загрузи файл.")
    elif data == "test":
        send_telegram_message(chat_id, "📝 Напиши тему, по которой создать тест.")
    elif data == "explain":
        send_telegram_message(chat_id, "🔍 Напиши понятие, которое нужно объяснить.")
    elif data == "premium" or data == "buy_premium":
        payment_link = f"https://your-tilda-site.ru/payment?user_id={user_id}"
        kb = {"inline_keyboard": [[{"text": "💳 Оплатить 150 руб", "url": payment_link}]]}
        send_telegram_message(chat_id,
                              "💳 **Оформление подписки**\n\n"
                              "Стоимость: **150 руб/мес**\n"
                              "После оплаты подписка активируется автоматически.\n"
                              "🔗 Нажми на кнопку ниже:",
                              json.dumps(kb))
    elif data == "referral":
        ref_link = f"https://t.me/unistudyhelper_bot?start=ref_{user_id}"
        send_telegram_message(chat_id,
                              f"🎁 **Реферальная программа**\n\n"
                              f"Твоя ссылка:\n`{ref_link}`\n\n"
                              "Приведи друга — получи скидку 20% на следующий месяц!")

# ================== HTTP ENDPOINTS ==================
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logging.info(f"Received update: {update}")
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
    status = data.get('status')

    if status == 'paid' and user_id:
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET subscription_end = ? WHERE user_id = ?", (end_date, user_id))
            cursor.execute("INSERT INTO payments (user_id, amount, payment_id, status) VALUES (?, ?, ?, ?)",
                           (user_id, amount, payment_id, status))
            conn.commit()
        send_telegram_message(user_id, f"✅ Оплата подтверждена! Подписка активна до {end_date}")
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid payment data"}), 400

# ================== ЗАПУСК ==================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)