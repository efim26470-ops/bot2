# app.py (расширенная система подписок)
import os
import logging
import json
import requests
import sqlite3
import time
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
        # Новая структура таблицы users
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
    """Возвращает информацию о пользователе из БД"""
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
    """Обновляет подписку пользователя"""
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
    """Для бесплатных пользователей обновляет requests_remaining до 5, если сегодня ещё не обновлялись"""
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
    """Проверяет, может ли пользователь сделать запрос (учитывая подписку и лимиты)"""
    if user_id in ADMIN_IDS:
        return True

    user = get_user_info(user_id)
    if not user:
        # Новый пользователь
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (user_id, requests_remaining, last_request_date) VALUES (?, 5, ?)", 
                           (user_id, datetime.now().date().isoformat()))
            conn.commit()
        return True

    # Если подписка активна
    if user["type"] != "free":
        if user["end_date"] and user["end_date"] >= datetime.now().date().isoformat():
            return user["remaining"] > 0
        else:
            # Подписка истекла – понижаем до free
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET subscription_type = 'free', subscription_end = NULL, requests_remaining = 5, last_request_date = ? WHERE user_id = ?",
                               (datetime.now().date().isoformat(), user_id))
                conn.commit()
            return True

    # Бесплатный пользователь – обновляем дневной лимит
    refresh_free_requests(user_id)
    user = get_user_info(user_id)  # обновляем данные
    return user["remaining"] > 0

def decrement_request(user_id: int):
    """Уменьшает счётчик запросов для пользователя"""
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
            ["🔢 Реши задачу", "🎨 Сгенерировать изображение"],
            ["⭐ Премиум", "🎁 Рефералка"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    send_telegram_message(chat_id, text, json.dumps(keyboard))

def remove_keyboard(chat_id: int, text: str):
    markup = {"remove_keyboard": True}
    send_telegram_message(chat_id, text, json.dumps(markup))

# ================== YANDEXGPT ИНТЕГРАЦИЯ ==================
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
    system = "Ты — эксперт по решению задач. Помоги пользователю решить задачу шаг за шагом. Если задача не указана, попроси её сформулировать."
    return call_yandexgpt(system, f"Реши задачу:\n{problem}")

# ================== YANDEXART (ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ) ==================
def generate_image(prompt: str, chat_id: int) -> str:
    url = "https://api.ai.yandex.net/art/v1/images/generation"
    headers = {
        "Authorization": f"Api-Key {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"art://{FOLDER_ID}/yandex-art/latest",
        "generationOptions": {"seed": 0},
        "messages": [{"role": "user", "text": prompt}]
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            logging.error(f"YandexART create error: {resp.status_code} {resp.text}")
            return None
        operation_id = resp.json().get("id")
        if not operation_id:
            return None
    except Exception as e:
        logging.error(f"YandexART create exception: {e}")
        return None

    status_url = f"https://api.ai.yandex.net/art/v1/images/generation/{operation_id}"
    for _ in range(15):
        time.sleep(2)
        try:
            status_resp = requests.get(status_url, headers=headers, timeout=10)
            if status_resp.status_code != 200:
                continue
            data = status_resp.json()
            if data.get("done"):
                image_base64 = data.get("response", {}).get("image")
                if image_base64:
                    return image_base64
                else:
                    return None
        except Exception as e:
            logging.error(f"YandexART status check error: {e}")
            continue
    return None

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

    # Регистрируем пользователя (если новый)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                       (user_id, username, first_name))
        conn.commit()

    # Если есть активное состояние
    state = user_states.get(user_id)
    if state:
        handle_state_input(user_id, chat_id, text, state)
        return

    # Обработка команд и кнопок
    if text == "/start":
        user = get_user_info(user_id)
        if user and user["type"] != "free":
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
                              "2. Введи текст или тему\n"
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
                              "Оплата производится через платёжную систему. После оплаты подписка активируется автоматически.",
                              json.dumps(kb))
        return

    # Кнопки обычной клавиатуры
    elif text in ["📝 Пересказать текст", "📝 Создать тест", "🔍 Объяснить понятие",
                  "✍️ Написать эссе", "🔢 Реши задачу", "🎨 Сгенерировать изображение"]:
        if not can_make_request(user_id):
            send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. Приобретите подписку: /premium")
            return
        # Определяем состояние
        mapping = {
            "📝 Пересказать текст": "summarize",
            "📝 Создать тест": "test",
            "🔍 Объяснить понятие": "explain",
            "✍️ Написать эссе": "essay",
            "🔢 Реши задачу": "solve_task",
            "🎨 Сгенерировать изображение": "generate_image"
        }
        state = mapping[text]
        prompts = {
            "summarize": "📄 Отправь текст, который нужно пересказать.",
            "test": "📝 Напиши тему, по которой создать тест.",
            "explain": "🔍 Напиши понятие, которое нужно объяснить.",
            "essay": "✍️ Напиши тему эссе.",
            "solve_task": "🔢 Напиши условие задачи.",
            "generate_image": "🎨 Напиши описание того, что нужно нарисовать."
        }
        remove_keyboard(chat_id, prompts[state])
        user_states[user_id] = state

    elif text == "⭐ Премиум":
        # Отправляем инлайн-кнопки с тарифами
        kb = {
            "inline_keyboard": [
                [{"text": "💎 Премиум (250 запросов/мес) — 150 руб", "callback_data": "buy_premium"}],
                [{"text": "💎 Премиум+ (500 запросов/мес) — 300 руб", "callback_data": "buy_premium_plus"}]
            ]
        }
        send_telegram_message(chat_id, "🌟 **Выберите тариф:**", json.dumps(kb))

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
    # Проверяем лимит ещё раз (на случай, если пользователь долго думал)
    if not can_make_request(user_id):
        send_telegram_message(chat_id, "⚠️ Лимит запросов исчерпан. Приобретите подписку: /premium")
        del user_states[user_id]
        send_main_keyboard(chat_id, "Главное меню:")
        return

    reply = None
    if state == "summarize":
        reply = summarize_text(text)
        save_query(user_id, text, reply)
    elif state == "test":
        reply = generate_test(text)
        save_query(user_id, text, reply)
    elif state == "explain":
        reply = explain_concept(text)
        save_query(user_id, text, reply)
    elif state == "essay":
        reply = generate_essay(text)
        save_query(user_id, text, reply)
    elif state == "solve_task":
        reply = solve_task(text)
        save_query(user_id, text, reply)
    elif state == "generate_image":
        send_telegram_message(chat_id, "🎨 Генерирую изображение, это может занять до 15 секунд...")
        img_base64 = generate_image(text, chat_id)
        if img_base64:
            try:
                image_data = base64.b64decode(img_base64)
                send_telegram_photo(chat_id, image_data, caption=f"🎨 *Ваше изображение*\nПромпт: {text[:100]}")
            except Exception as e:
                logging.error(f"Image send error: {e}")
                reply = "⚠️ Не удалось отправить изображение. Попробуйте позже."
        else:
            reply = "⚠️ Не удалось сгенерировать изображение. Проверьте, что у сервисного аккаунта есть роль `ai.art.user`, или попробуйте другой промпт."
        if reply:
            save_query(user_id, text, reply)
    else:
        reply = "Неизвестное действие."

    # Уменьшаем счётчик запросов, если это не изображение или изображение успешно отправлено
    if state != "generate_image" or (state == "generate_image" and not img_base64):
        decrement_request(user_id)

    if reply:
        send_telegram_message(chat_id, reply)

    # Сбрасываем состояние и возвращаем клавиатуру
    if user_id in user_states:
        del user_states[user_id]
    send_main_keyboard(chat_id, "Что ещё сделать?")

# ================== CALLBACK-ОБРАБОТЧИКИ (инлайн-кнопки) ==================
def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    user_id = callback["from"]["id"]
    data = callback["data"]
    # Отвечаем на callback
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
    else:
        return

    # Ссылка на платёжную страницу (замените на реальную)
    payment_link = f"https://your-tilda-site.ru/payment?user_id={user_id}&plan={plan}"
    kb = {"inline_keyboard": [[{"text": f"💳 Оплатить {amount} руб", "url": payment_link}]]}
    send_telegram_message(chat_id,
                          f"💳 **Оформление подписки {plan.upper()}**\n\n"
                          f"Стоимость: **{amount} руб/мес**\n"
                          f"Количество запросов: **{requests_limit}** в месяц\n\n"
                          "После оплаты подписка активируется автоматически.\n"
                          "🔗 Нажми на кнопку ниже:",
                          json.dumps(kb))

# ================== ВЕБХУК ==================
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    logging.info(f"Received update: {update}")
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    else:
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