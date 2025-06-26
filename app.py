import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# LINE Bot SDK 版本檢測和導入
LINEBOT_SDK_VERSION = 2  # 預設使用 v2
try:
    from linebot.v3.webhook import WebhookHandler
    from linebot.v3.exceptions import InvalidSignatureError
    from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, PushMessageRequest
    from linebot.v3.webhooks import MessageEvent, TextMessageContent
    LINEBOT_SDK_VERSION = 3
    logger.info("成功導入 LINE Bot SDK v3")
except ImportError:
    from linebot import LineBotApi, WebhookHandler
    from linebot.exceptions import InvalidSignatureError
    from linebot.models import MessageEvent, TextMessage, TextSendMessage
    logger.info("回退到 LINE Bot SDK v2")

import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import re
from threading import Timer
import atexit
from calendar import monthrange

app = Flask(__name__)

# LINE Bot 設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.error("缺少 LINE Bot 環境變數")
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET 環境變數")

# LINE Bot API 初始化
if LINEBOT_SDK_VERSION == 3:
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        api_client = ApiClient(configuration)
        line_bot_api = MessagingApi(api_client)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
        logger.info("LINE Bot SDK v3 初始化成功")
    except Exception as e:
        logger.error(f"LINE Bot SDK v3 初始化失敗，回退到 v2: {e}")
        LINEBOT_SDK_VERSION = 2

if LINEBOT_SDK_VERSION == 2:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    logger.info("LINE Bot SDK v2 初始化成功")

# Google Sheets 設定
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

USE_GOOGLE_SHEETS = bool(GOOGLE_CREDENTIALS and SPREADSHEET_ID)
if not USE_GOOGLE_SHEETS:
    logger.warning("未設定 Google Sheets 環境變數，使用記憶體模式")

# 時區和記憶體儲存
TZ = pytz.timezone('Asia/Taipei')
memory_storage = []

class ScheduleManager:
    def __init__(self):
        self.gc = None
        self.sheet = None
        if USE_GOOGLE_SHEETS:
            self.setup_google_sheets()
    
    def setup_google_sheets(self):
        try:
            credentials_dict = json.loads(GOOGLE_CREDENTIALS)
            creds = Credentials.from_service_account_info(
                credentials_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open_by_key(SPREADSHEET_ID).sheet1
            
            headers = ['ID', '日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID', '狀態']
            try:
                existing_headers = self.sheet.row_values(1)
                if not existing_headers or len(existing_headers) < len(headers):
                    if existing_headers:
                        self.sheet.update('A1:H1', [headers])
                    else:
                        self.sheet.insert_row(headers, 1)
                logger.info("Google Sheets 表頭設定完成")
            except Exception as e:
                logger.error(f"設定表頭錯誤: {e}")
                
            logger.info("Google Sheets 連接成功")
            
        except Exception as e:
            logger.error(f"Google Sheets 連接失敗: {e}")
            raise
    
    def add_schedule(self, date_str, time_str, content, user_id, reminder=None):
        try:
            schedule_date = datetime.strptime(date_str, '%Y-%m-%d')
            
            if time_str:
                datetime.strptime(time_str, '%H:%M')
                
            today = datetime.now(TZ).date()
            if schedule_date.date() < today:
                logger.warning(f"嘗試新增過去的日期: {date_str}")
                return "過去日期"
            
            created_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            
            if USE_GOOGLE_SHEETS and self.sheet:
                try:
                    schedule_id = f"S{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    row = [schedule_id, date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
                    self.sheet.append_row(row)
                    logger.info(f"成功寫入 Google Sheets: {schedule_id}")
                    
                except Exception as e:
                    logger.error(f"寫入 Google Sheets 失敗: {e}")
                    schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    schedule = {
                        'ID': schedule_id, '日期': date_str, '時間': time_str or '', '行程內容': content,
                        '提醒設定': reminder or '', '建立時間': created_time, 'LINE用戶ID': user_id, '狀態': '有效'
                    }
                    memory_storage.append(schedule)
                    logger.info(f"回退到記憶體模式: {schedule_id}")
            else:
                schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                schedule = {
                    'ID': schedule_id, '日期': date_str, '時間': time_str or '', '行程內容': content,
                    '提醒設定': reminder or '', '建立時間': created_time, 'LINE用戶ID': user_id, '狀態': '有效'
                }
                memory_storage.append(schedule)
            
            logger.info(f"成功新增行程: {user_id} - {date_str} {time_str} {content} (ID: {schedule_id})")
            return schedule_id
            
        except ValueError as e:
            logger.error(f"日期時間格式錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"新增行程失敗: {e}")
            return False
    
    def get_schedules_by_date_range(self, start_date, end_date, user_id=None):
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            schedules = []
            for record in all_records:
                if not record.get('日期') or not record.get('行程內容'):
                    continue
                if record.get('狀態') == '已刪除':
                    continue
                if user_id and record.get('LINE用戶ID') != user_id:
                    continue
                
                try:
                    schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                    if start_date <= schedule_date <= end_date:
                        schedules.append(record)
                except ValueError:
                    logger.warning(f"日期格式錯誤: {record.get('日期')}")
                    continue
            
            return sorted(schedules, key=lambda x: (x['日期'], x.get('時間', '')))
        except Exception as e:
            logger.error(f"取得行程失敗: {e}")
            return []
    
    def get_today_schedules(self, user_id):
        today = datetime.now(TZ).date()
        return self.get_schedules_by_date_range(today, today, user_id)
    
    def get_tomorrow_schedules(self, user_id):
        tomorrow = datetime.now(TZ).date() + timedelta(days=1)
        return self.get_schedules_by_date_range(tomorrow, tomorrow, user_id)
    
    def get_this_week_schedules(self, user_id):
        today = datetime.now(TZ).date()
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        this_sunday = this_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(this_monday, this_sunday, user_id)
    
    def get_next_week_schedules(self, user_id):
        today = datetime.now(TZ).date()
        days_until_next_monday = 7 - today.weekday()
        next_monday = today + timedelta(days=days_until_next_monday)
        next_sunday = next_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(next_monday, next_sunday, user_id)
    
    def get_this_month_schedules(self, user_id):
        today = datetime.now(TZ).date()
        this_month_start = today.replace(day=1)
        _, last_day = monthrange(today.year, today.month)
        this_month_end = today.replace(day=last_day)
        return self.get_schedules_by_date_range(this_month_start, this_month_end, user_id)
    
    def get_next_month_schedules(self, user_id):
        today = datetime.now(TZ).date()
        if today.month == 12:
            next_month_start = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month_start = today.replace(month=today.month + 1, day=1)
        
        year = next_month_start.year
        month = next_month_start.month
        _, last_day = monthrange(year, month)
        next_month_end = next_month_start.replace(day=last_day)
        return self.get_schedules_by_date_range(next_month_start, next_month_end, user_id)
    
    def get_next_year_schedules(self, user_id):
        today = datetime.now(TZ).date()
        next_year_start = today.replace(year=today.year + 1, month=1, day=1)
        next_year_end = today.replace(year=today.year + 1, month=12, day=31)
        return self.get_schedules_by_date_range(next_year_start, next_year_end, user_id)
    
    def get_recent_schedules(self, user_id, days=7):
        today = datetime.now(TZ).date()
        end_date = today + timedelta(days=days-1)
        return self.get_schedules_by_date_range(today, end_date, user_id)
    
    def get_schedule_by_id(self, schedule_id, user_id=None):
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            for record in all_records:
                if (record.get('ID') == schedule_id and record.get('狀態') != '已刪除'):
                    if user_id and record.get('LINE用戶ID') != user_id:
                        return None
                    return record
            return None
        except Exception as e:
            logger.error(f"查詢行程 ID 失敗: {e}")
            return None
    
    def get_user_schedules_with_id(self, user_id, limit=10):
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            user_schedules = []
            for record in all_records:
                if (record.get('LINE用戶ID') == user_id and record.get('狀態') != '已刪除'):
                    user_schedules.append(record)
            
            user_schedules.sort(key=lambda x: x.get('建立時間', ''), reverse=True)
            return user_schedules[:limit]
        except Exception as e:
            logger.error(f"查詢用戶行程失敗: {e}")
            return []
    
    def delete_schedule_by_id(self, schedule_id, user_id):
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
                row_num = 2
                for record in all_records:
                    if (record.get('ID') == schedule_id and 
                        record.get('LINE用戶ID') == user_id and 
                        record.get('狀態') != '已刪除'):
                        self.sheet.update(f'H{row_num}', '已刪除')
                        logger.info(f"成功刪除行程 ID: {schedule_id}")
                        return record
                    row_num += 1
            else:
                for record in memory_storage:
                    if (record.get('ID') == schedule_id and 
                        record.get('LINE用戶ID') == user_id and 
                        record.get('狀態') != '已刪除'):
                        record['狀態'] = '已刪除'
                        logger.info(f"成功刪除行程 ID: {schedule_id}")
                        return record
            return None
        except Exception as e:
            logger.error(f"刪除行程 ID 失敗: {e}")
            return None

schedule_manager = ScheduleManager()

def format_schedules(schedules, title):
    if not schedules:
        return f"{title}\n📅 目前沒有安排任何行程"
    
    message = f"{title}\n"
    current_date = None
    
    for schedule in schedules:
        date = schedule.get('日期', '')
        time = schedule.get('時間', '') or '全天'
        content = schedule.get('行程內容', '')
        
        if date != current_date:
            if current_date is not None:
                message += "\n"
            current_date = date
            
            try:
                date_obj = datetime.strptime(date, '%Y-%m-%d')
                weekday = ['一', '二', '三', '四', '五', '六', '日'][date_obj.weekday()]
                formatted_date = f"{date_obj.month}/{date_obj.day} (週{weekday})"
                message += f"📅 {formatted_date}\n"
            except:
                message += f"📅 {date}\n"
        
        if time != '全天':
            message += f"   ⏰ {time} - {content}\n"
        else:
            message += f"   📝 {content} (全天)\n"
    
    return message.strip()

def is_schedule_input(text):
    patterns = [
        r'\d{1,2}/\d{1,2}', r'\d{1,2}月\d{1,2}[號日]', r'\d{4}-\d{1,2}-\d{1,2}',
        r'今天.*\d{1,2}[點時]', r'明天.*\d{1,2}[點時]', r'後天.*\d{1,2}[點時]'
    ]
    return any(re.search(pattern, text) for pattern in patterns)

def parse_schedule_input(text):
    content = text.replace('新增行程', '').strip()
    if not content:
        return None, None, None
    
    date_str, time_str, schedule_content = parse_natural_input(content)
    if date_str and schedule_content:
        return date_str, time_str, schedule_content
    return None, None, None

def parse_natural_input(text):
    current_year = datetime.now().year
    today = datetime.now(TZ).date()
    
    patterns = [
        (r'今天\s*(\d{1,2})[點時]\s*(.+)', 'today_time'),
        (r'今天\s*上午(\d{1,2})[點時]\s*(.+)', 'today_am'),
        (r'今天\s*下午(\d{1,2})[點時]\s*(.+)', 'today_pm'),
        (r'今天\s*晚上(\d{1,2})[點時]\s*(.+)', 'today_pm'),
        (r'今天\s*(.+)', 'today_only'),
        (r'明天\s*(\d{1,2})[點時]\s*(.+)', 'tomorrow_time'),
        (r'明天\s*上午(\d{1,2})[點時]\s*(.+)', 'tomorrow_am'),
        (r'明天\s*下午(\d{1,2})[點時]\s*(.+)', 'tomorrow_pm'),
        (r'明天\s*晚上(\d{1,2})[點時]\s*(.+)', 'tomorrow_pm'),
        (r'明天\s*(.+)', 'tomorrow_only'),
        (r'後天\s*(\d{1,2})[點時]\s*(.+)', 'day_after_tomorrow_time'),
        (r'後天\s*上午(\d{1,2})[點時]\s*(.+)', 'day_after_tomorrow_am'),
        (r'後天\s*下午(\d{1,2})[點時]\s*(.+)', 'day_after_tomorrow_pm'),
        (r'後天\s*晚上(\d{1,2})[點時]\s*(.+)', 'day_after_tomorrow_pm'),
        (r'後天\s*(.+)', 'day_after_tomorrow_only'),
        (r'(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)', 'date_time'),
        (r'(\d{1,2})/(\d{1,2})\s+(.+)', 'date_only'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*下午(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*上午(\d{1,2})[點時]\s*(.+)', 'chinese_am'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*晚上(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(\d{1,2})[點時]\s*(.+)', 'chinese_default'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(.+)', 'chinese_date_only')
    ]
    
    for pattern, pattern_type in patterns:
        match = re.match(pattern, text.strip())
        if match:
            try:
                if pattern_type.startswith('today'):
                    target_date = today
                elif pattern_type.startswith('tomorrow'):
                    target_date = today + timedelta(days=1)
                elif pattern_type.startswith('day_after_tomorrow'):
                    target_date = today + timedelta(days=2)
                else:
                    target_date = None
                
                if target_date:
                    date_str = target_date.strftime('%Y-%m-%d')
                    if pattern_type.endswith('_time'):
                        hour, content = match.groups()
                        hour = int(hour)
                        if hour > 24:
                            continue
                        time_str = f"{hour:02d}:00"
                        return date_str, time_str, content.strip()
                    elif pattern_type.endswith('_am'):
                        hour, content = match.groups()
                        hour = int(hour)
                        if hour == 12:
                            hour = 0
                        if hour > 12:
                            continue
                        time_str = f"{hour:02d}:00"
                        return date_str, time_str, content.strip()
                    elif pattern_type.endswith('_pm'):
                        hour, content = match.groups()
                        hour = int(hour)
                        if hour < 12:
                            hour += 12
                        if hour > 24:
                            continue
                        time_str = f"{hour:02d}:00"
                        return date_str, time_str, content.strip()
                    elif pattern_type.endswith('_only'):
                        content = match.groups()[0]
                        return date_str, '', content.strip()
                
                elif pattern_type == 'date_time':
                    month, day, hour, minute, content = match.groups()
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{int(hour):02d}:{minute}"
                    return date_str, time_str, content.strip()
                elif pattern_type == 'date_only':
                    month, day, content = match.groups()
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                elif pattern_type == 'chinese_pm':
                    month, day, hour, content = match.groups()
                    hour = int(hour)
                    if hour < 12:
                        hour += 12
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                elif pattern_type == 'chinese_am':
                    month, day, hour, content = match.groups()
                    hour = int(hour)
                    if hour == 12:
                        hour = 0
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                elif pattern_type == 'chinese_default':
                    month, day, hour, content = match.groups()
                    hour = int(hour)
                    if hour > 24:
                        continue
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                elif pattern_type == 'chinese_date_only':
                    month, day, content = match.groups()
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                    
            except (ValueError, IndexError):
                continue
    
    return None, None, None

@app.route("/", methods=["GET"])
def health_check():
    status = "記憶體模式" if not USE_GOOGLE_SHEETS else "Google Sheets 模式"
    return f"LINE Bot 行程管理系統運行中 ({status})", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        logger.warning("缺少 X-Line-Signature 標頭")
        abort(400)
        
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=(TextMessageContent if LINEBOT_SDK_VERSION == 3 else TextMessage))
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    try:
        # 倒數計時功能
        if text.startswith("倒數") and "分鐘" in text:
            try:
                minute = int(re.search(r'\d+', text).group())
                if 0 < minute <= 60:
                    reply_text = f"⏰ 倒數 {minute} 分鐘開始！我會在時間到時提醒你。"
                    
                    if hasattr(event.source, 'group_id') and event.source.group_id:
                        target_id = event.source.group_id
                    elif hasattr(event.source, 'room_id') and event.source.room_id:
                        target_id = event.source.room_id
                    else:
                        target_id = event.source.user_id
                    
                    def send_reminder():
                        try:
                            reminder_text = f"⏰ {minute} 分鐘倒數結束，時間到囉！"
                            if LINEBOT_SDK_VERSION == 3:
                                push_message = TextMessage(text=reminder_text)
                                line_bot_api.push_message(PushMessageRequest(to=target_id, messages=[push_message]))
                            else:
                                line_bot_api.push_message(target_id, TextSendMessage(text=reminder_text))
                            logger.info(f"成功發送倒數提醒: {minute} 分鐘")
                        except Exception as e:
                            logger.error(f"推送提醒失敗: {e}")
                    
                    Timer(minute * 60, send_reminder).start()
                    
                    if LINEBOT_SDK_VERSION == 3:
                        reply_message = TextMessage(text=reply_text)
                        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_message]))
                    else:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                    return
                else:
                    reply_text = "⚠️ 倒數時間請設定在 1-60 分鐘之間"
            except (ValueError, AttributeError):
                reply_text = "❌ 請輸入正確格式：倒數 X 分鐘，例如：倒數 5 分鐘"
        
        # 查詢功能
        elif text == "今日行程":
            schedules = schedule_manager.get_today_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 今日行程")
        elif text == "明日行程":
            schedules = schedule_manager.get_tomorrow_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 明日行程")
        elif text == "本週行程":
            schedules = schedule_manager.get_this_week_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 本週行程")
        elif text == "下週行程":
            schedules = schedule_manager.get_next_week_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 下週行程")
        elif text == "本月行程":
            schedules = schedule_manager.get_this_month_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 本月行程")
        elif text == "下個月行程":
            schedules = schedule_manager.get_next_month_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 下個月行程")
        elif text == "明年行程":
            schedules = schedule_manager.get_next_year_schedules(user_id)
            reply_text = format_schedules(schedules, "📅 明年行程")
        elif text == "近期行程":
            schedules = schedule_manager.get_recent_schedules(user_id, 7)
            reply_text = format_schedules(schedules, "📅 近期行程（7天內）")
        
        # ID 查詢功能
        elif text.startswith("查詢ID") or text.startswith("查詢id"):
            content = text.replace('查詢ID', '').replace('查詢id', '').strip()
            if content:
                schedule = schedule_manager.get_schedule_by_id(content, user_id)
                if schedule:
                    date = schedule.get('日期', '')
                    time = schedule.get('時間', '') or '全天'
                    content_text = schedule.get('行程內容', '')
                    created_time = schedule.get('建立時間', '')
                    schedule_id = schedule.get('ID', '')
                    
                    try:
                        date_obj = datetime.strptime(date, '%Y-%m-%d')
                        weekday = ['一', '二', '三', '四', '五', '六', '日'][date_obj.weekday()]
                        friendly_date = f"{date_obj.month}/{date_obj.day} (週{weekday})"
                    except:
                        friendly_date = date
                    
                    if time != '全天':
                        reply_text = f"🔍 行程詳細資訊\n\n🆔 ID: {schedule_id}\n📅 日期: {friendly_date}\n⏰ 時間: {time}\n📝 內容: {content_text}\n🕐 建立時間: {created_time}"
                    else:
                        reply_text = f"🔍 行程詳細資訊\n\n🆔 ID: {schedule_id}\n📅 日期: {friendly_date} (全天)\n📝 內容: {content_text}\n🕐 建立時間: {created_time}"
                else:
                    reply_text = f"❌ 找不到行程 ID: {content}\n請確認 ID 是否正確，或該行程是否為您建立的"
            else:
                reply_text = "❌ 請輸入要查詢的行程 ID，格式：查詢ID S20240101120000001"
        
        # 我的行程列表
        elif text in ["我的行程", "行程列表", "行程ID"]:
            schedules = schedule_manager.get_user_schedules_with_id(user_id, 10)
            if schedules:
                reply_text = "📋 您的行程列表（最近10筆）\n\n"
                for i, schedule in enumerate(schedules, 1):
                    date = schedule.get('日期', '')
                    time = schedule.get('時間', '') or '全天'
                    content = schedule.get('行程內容', '')
                    schedule_id = schedule.get('ID', '')
                    
                    try:
                        date_obj = datetime.strptime(date, '%Y-%m-%d')
                        friendly_date = f"{date_obj.month}/{date_obj.day}"
                    except:
                        friendly_date = date
                    
                    if time != '全天':
                        reply_text += f"{i}. 📅 {friendly_date} {time}\n   📝 {content}\n   🆔 {schedule_id}\n\n"
                    else:
                        reply_text += f"{i}. 📅 {friendly_date} (全天)\n   📝 {content}\n   🆔 {schedule_id}\n\n"
                
                reply_text += "💡 使用「查詢ID [ID號碼]」查看詳細資訊\n💡 使用「刪除ID [ID號碼]」刪除特定行程"
            else:
                reply_text = "📋 您目前沒有任何行程\n\n💡 輸入「今天10點開會」開始新增行程"
        
        # 新增行程功能
        elif text.startswith("新增行程") or is_schedule_input(text):
            if not text.startswith("新增行程"):
                text = "新增行程 " + text
                
            date_str, time_str, content = parse_schedule_input(text)
            
            if date_str and content:
                success = schedule_manager.add_schedule(date_str, time_str, content, user_id)
                if isinstance(success, str) and success.startswith(('S', 'M')):
                    time_display = f" {time_str}" if time_str else " (全天)"
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        weekday = ['一', '二', '三', '四', '五', '六', '日'][date_obj.weekday()]
                        friendly_date = f"{date_obj.month}/{date_obj.day} (週{weekday})"
                        reply_text = f"✅ 行程已新增成功！\n📅 {friendly_date}{time_display}\n📝 {content}\n🆔 行程ID: {success}"
                    except:
                        reply_text = f"✅ 行程已新增成功！\n📅 {date_str}{time_display}\n📝 {content}\n🆔 行程ID: {success}"
                elif success == "過去日期":
                    reply_text = "⚠️ 無法新增過去的日期，請選擇今天或未來的日期"
                else:
                    reply_text = "❌ 新增行程失敗，請檢查日期格式是否正確或稍後再試"
            else:
                reply_text = ("❌ 格式錯誤！支援以下格式：\n\n"
                             "📝 快速輸入：\n"
                             "• 今天10點開會\n"
                             "• 明天下午2點聚餐\n"
                             "• 後天上午9點會議\n"
                             "• 7/14 10:00 開會\n"
                             "• 6月30號 下午2點 盤點\n"
                             "• 12月25號 聖誕節")
        
        # 刪除ID功能
        elif text.startswith("刪除ID") or text.startswith("刪除id"):
            content = text.replace('刪除ID', '').replace('刪除id', '').strip()
            if content:
                deleted_schedule = schedule_manager.delete_schedule_by_id(content, user_id)
                if deleted_schedule:
                    date = deleted_schedule.get('日期', '')
                    content_text = deleted_schedule.get('行程內容', '')
                    try:
                        date_obj = datetime.strptime(date, '%Y-%m-%d')
                        weekday = ['一', '二', '三', '四', '五', '六', '日'][date_obj.weekday()]
                        friendly_date = f"{date_obj.month}/{date_obj.day} (週{weekday})"
                    except:
                        friendly_date = date
                    reply_text = f"✅ 已成功刪除行程\n📅 {friendly_date}\n📝 {content_text}\n🆔 ID: {content}"
                else:
                    reply_text = f"❌ 找不到行程 ID: {content}\n請確認 ID 是否正確，或該行程是否已被刪除"
            else:
                reply_text = "❌ 請輸入要刪除的行程 ID，格式：刪除ID S20240101120000001"
        
        # 功能說明
        elif text in ["功能", "menu", "選單", "菜單"]:
            reply_text = ("🎯 功能選單\n\n"
                         "📝 新增行程：直接輸入「今天10點開會」\n"
                         "🔍 查詢行程：「今日行程」「明日行程」等\n"
                         "🆔 管理行程：「我的行程」查看所有行程ID\n"
                         "⏰ 倒數計時：「倒數 5 分鐘」\n"
                         "🔧 系統狀態：「狀態」\n\n"
                         "💡 快速範例：\n"
                         "• 明天10點開會\n"
                         "• 查詢ID S123...\n"
                         "• 刪除ID S123...")
        
        elif text in ["幫助", "help", "使用說明", "?"]:
            reply_text = ("🤖 LINE Bot 行程管理系統\n\n"
                         "⚡ 快速使用：\n"
                         "• 明天10點開會 - 新增行程\n"
                         "• 今日行程 - 查詢今天行程\n"
                         "• 我的行程 - 查看所有行程及ID\n"
                         "• 倒數 5 分鐘 - 開始倒數計時\n\n"
                         "💡 輸入「功能」查看完整選單")
        
        elif text in ["狀態", "系統狀態", "status"]:
            try:
                if USE_GOOGLE_SHEETS and schedule_manager.sheet:
                    test_records = schedule_manager.sheet.get_all_records()
                    sheets_status = "✅ Google Sheets 正常"
                    total_records = len([r for r in test_records if r.get('行程內容')])
                    user_records = len([r for r in test_records if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
                else:
                    sheets_status = "📱 記憶體模式"
                    total_records = len([r for r in memory_storage if r.get('行程內容')])
                    user_records = len([r for r in memory_storage if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
            except:
                sheets_status = "❌ 連接異常"
                total_records = 0
                user_records = 0
            
            reply_text = (f"🔧 系統狀態報告\n\n"
                         f"📊 資料儲存: {sheets_status}\n"
                         f"📈 總行程數: {total_records}\n"
                         f"👤 您的行程數: {user_records}\n"
                         f"🕐 系統時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                         f"🌏 時區: Asia/Taipei")
        
        elif text.startswith("倒數"):
            reply_text = "❌ 請輸入正確格式：倒數 X 分鐘，例如：倒數 5 分鐘（1-60分鐘）"
        
        else:
            reply_text = ("🤔 我不太理解您的指令\n\n"
                         "請輸入「幫助」查看使用說明，或直接輸入行程資訊\n"
                         "例如：今天10點開會、7/14 聚餐")
        
        # 發送回覆
        if LINEBOT_SDK_VERSION == 3:
            reply_message = TextMessage(text=reply_text)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_message]))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    
    except Exception as e:
        error_msg = f"處理訊息時發生錯誤: {str(e)}"
        logger.error(error_msg)
        try:
            error_text = "系統發生異常，請稍後再試"
            if LINEBOT_SDK_VERSION == 3:
                error_reply = TextMessage(text=error_text)
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[error_reply]))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_text))
        except:
            pass

# 排程器設定
scheduler = BackgroundScheduler(timezone=TZ)

def friday_reminder():
    try:
        logger.info("週五提醒功能執行")
    except Exception as e:
        logger.error(f"週五提醒執行失敗: {e}")

scheduler.add_job(friday_reminder, 'cron', day_of_week='fri', hour=10, minute=0, id='friday_reminder')

def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("排程器已關閉")

atexit.register(shutdown_scheduler)

@app.errorhandler(404)
def not_found(error):
    return "Not Found", 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return "Internal Server Error", 500

if __name__ == "__main__":
    try:
        scheduler.start()
        logger.info("排程器已啟動")
        
        if USE_GOOGLE_SHEETS:
            try:
                test_records = schedule_manager.sheet.get_all_records()
                logger.info(f"Google Sheets 連接測試成功，共 {len(test_records)} 筆記錄")
            except Exception as e:
                logger.error(f"Google Sheets 連接測試失敗: {e}")
        else:
            logger.info("使用記憶體模式運行")
        
        port = int(os.environ.get("PORT", 3000))
        logger.info(f"LINE Bot 行程管理系統啟動，監聽端口: {port}")
        app.run(host="0.0.0.0", port=port, debug=False)
        
    except KeyboardInterrupt:
        logger.info("接收到中斷信號，正在關閉系統...")
        shutdown_scheduler()
    except Exception as e:
        logger.error(f"應用程式啟動失敗: {e}")
        shutdown_scheduler()
        raise
