# app.py
import os
import logging
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

# ================== КОНФИГУРАЦИЯ ==================
# Получаем переменные окружения (задаются в Railway)
FOLDER_ID = os.environ.get('FOLDER_ID')
API_KEY = os.environ.get('API_KEY')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.environ.get('ADMIN_IDS', '').split(',') if id.strip()]  # список ID через запятую
DATABASE_URL = os.environ.get('DATABASE_URL')  # PostgreSQL URL от Railway

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Please add PostgreSQL plugin or set DATABASE_URL manually.")

# ================== ИНИЦИАЛИЗАЦИЯ ==================
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ================== БАЗА ДАННЫХ (POSTGRESQL) ==================
def get_db_connection():
    """Возвращает новое соединение с PostgreSQL"""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Создаёт таблицы, если их нет"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    subscription_end DATE,
                    requests_today INTEGER DEFAULT 0,
                    last_request_date DATE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS queries (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    query_text TEXT,
                    response_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount INTEGER,
                    payment_id TEXT,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        conn.commit()
        logging.info("Database initialized")

# Вызовем при старте
init_db()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def is_premium(user_id: int) -> bool:
    """Проверка активной подписки"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subscription_end FROM users WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            if result and result[0]:
                end_date = result[0]
                return end_date >= datetime.now().date()
            return False

def increment_requests(user_id: int) -> int:
    """Увеличивает счётчик запросов и возвращает текущее количество за сегодня"""
    today = datetime.now().date().isoformat()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT requests_today, last_request_date FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                last_date = row[1]
                if last_date != today:
                    cur.execute("UPDATE users SET requests_today = 1, last_request_date = %s WHERE user_id = %s", (today, user_id))
                    conn.commit()
                    return 1
                else:
                    cur.execute("UPDATE users SET requests_today = requests_today + 1 WHERE user_id = %s", (user_id,))
                    conn.commit()
                    cur.execute("SELECT requests_today FROM users WHERE user_id = %s", (user_id,))
                    count = cur.fetchone()[0]
                    return count
            else:
                cur.execute("INSERT INTO users (user_id, requests_today, last_request_date) VALUES (%s, 1, %s)", (user_id, today))
                conn.commit()
                return 1

def can_make_request(user_id: int) -> bool:
    """Проверяет, может ли пользователь сделать запрос (премиум или лимит 5 в день)"""
    if is_premium(user_id):
        return True
    return increment_requests(user_id) <= 5

def save_query(user_id: int, query: str, response: str):
    """Сохраняет запрос и ответ в историю"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO queries (user_id, query_text, response_text) VALUES (%s, %s, %s)",
                        (user_id, query[:500], response[:500]))
        conn.commit()

def send_telegram_message(chat_id: int, text: str, reply_markup=None):
    """Отправляет сообщение в Telegram"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

# ================== YANDEXGPT ИНТЕГРАЦИЯ ==================
def call_yandexgpt(system_prompt: str, user_message: str) -> str:
    """Вызывает YandexGPT и возвращает ответ"""
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
    """Основной обработчик обновлений от Telegram"""
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
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
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

    # Обработка callback-запросов (кнопки)
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    # Обычное сообщение — считаем запросом к AI
    if not can_make_request(user_id):
        send_telegram_message(chat_id,
                              "⚠️ Ты исчерпал лимит бесплатных запросов на сегодня.\n"
                              "Оформи премиум за 150 руб/мес для безлимита!\n"
                              "/premium")
        return

    # Генерация ответа по умолчанию (можно расширить)
    system_prompt = "Ты полезный ассистент. Отвечай кратко и по делу."
    reply = call_yandexgpt(system_prompt, text)
    save_query(user_id, text, reply)
    send_telegram_message(chat_id, reply)

def handle_callback(callback):
    """Обрабатывает нажатия инлайн-кнопок"""
    chat_id = callback["message"]["chat"]["id"]
    user_id = callback["from"]["id"]
    data = callback["data"]
    # Подтверждаем получение callback'а
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                  json={"callback_query_id": callback["id"]})

    if data == "conspect":
        send_telegram_message(chat_id, "📚 Отправь текст лекции или загрузи файл.")
        # В реальном боте здесь нужно перевести пользователя в состояние ожидания текста
        # Для упрощения оставим так, но пользователь должен будет написать текст вручную.
        # В полноценной версии используйте FSM (например, через хранение состояния в БД).
    elif data == "test":
        send_telegram_message(chat_id, "📝 Напиши тему, по которой создать тест.")
    elif data == "explain":
        send_telegram_message(chat_id, "🔍 Напиши понятие, которое нужно объяснить.")
    elif data == "premium" or data == "buy_premium":
        # Здесь должна быть ссылка на ваш платёжный сервис (Tilda, ЮKassa и т.п.)
        payment_link = f"https://your-tilda-site.ru/payment?user_id={user_id}"
        kb = {"inline_keyboard": [[{"text": "💳 Оплатить 150 руб", "url": payment_link}]]}
        send_telegram_message(chat_id,
                              "💳 **Оформление подписки**\n\n"
                              "Стоимость: **150 руб/мес**\n"
                              "После оплаты подписка активируется автоматически.\n"
                              "🔗 Нажми на кнопку ниже:",
                              json.dumps(kb))
    elif data == "referral":
        ref_link = f"https://t.me/StudyHelperBot?start=ref_{user_id}"
        send_telegram_message(chat_id,
                              f"🎁 **Реферальная программа**\n\n"
                              f"Твоя ссылка:\n`{ref_link}`\n\n"
                              "Приведи друга — получи скидку 20% на следующий месяц!")

# ================== HTTP ENDPOINTS ==================
@app.route('/webhook', methods=['POST'])
def webhook():
    """Эндпоинт для Telegram вебхука"""
    update = request.get_json()
    logging.info(f"Received update: {update}")
    handle_telegram_update(update)
    return "OK", 200

@app.route('/chat', methods=['POST'])
def chat():
    """Оставлен для совместимости с предыдущим интерфейсом (прямой вызов)"""
    data = request.get_json()
    user_msg = data.get('message', '').strip()
    if not user_msg:
        return jsonify({"reply": "Напишите что-нибудь"}), 400
    reply = call_yandexgpt("Ты полезный ассистент", user_msg)
    return jsonify({"reply": reply})

@app.route('/payment-webhook', methods=['POST'])
def payment_webhook():
    """
    Принимает уведомления от платёжного конструктора.
    Ожидает JSON: {"user_id": 123, "payment_id": "xxx", "amount": 150, "status": "paid"}
    """
    data = request.get_json()
    user_id = data.get('user_id')
    payment_id = data.get('payment_id')
    amount = data.get('amount')
    status = data.get('status')

    if status == 'paid' and user_id:
        end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET subscription_end = %s WHERE user_id = %s", (end_date, user_id))
                cur.execute("INSERT INTO payments (user_id, amount, payment_id, status) VALUES (%s, %s, %s, %s)",
                            (user_id, amount, payment_id, status))
            conn.commit()
        send_telegram_message(user_id, f"✅ Оплата подтверждена! Подписка активна до {end_date}")
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid payment data"}), 400

# ================== ЗАПУСК ==================
if __name__ == '__main__':
    # Для локального запуска или если не используется gunicorn
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)