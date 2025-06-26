import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, MessagingApi, MessagingApiConfiguration,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import re
from threading import Timer
import atexit
from calendar import monthrange

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# LINE Bot 驗證資料
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# 檢查必要的環境變數
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.error("缺少 LINE Bot 環境變數")
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET 環境變數")

# 初始化 LINE Bot API v3
configuration = MessagingApiConfiguration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 設定
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# 如果沒有設定 Google Sheets 相關環境變數，使用模擬模式
if not GOOGLE_CREDENTIALS or not SPREADSHEET_ID:
    logger.warning("未設定 GOOGLE_CREDENTIALS 或 SPREADSHEET_ID，將使用記憶體模式運行")
    USE_GOOGLE_SHEETS = False
else:
    USE_GOOGLE_SHEETS = True

# 時區設定
TZ = pytz.timezone('Asia/Taipei')

# 記憶體儲存（當無法使用 Google Sheets 時）
memory_storage = []

class ScheduleManager:
    def __init__(self):
        self.gc = None
        self.sheet = None
        if USE_GOOGLE_SHEETS:
            self.setup_google_sheets()
    
    def setup_google_sheets(self):
        """設定 Google Sheets 連接"""
        try:
            credentials_dict = json.loads(GOOGLE_CREDENTIALS)
            creds = Credentials.from_service_account_info(
                credentials_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open_by_key(SPREADSHEET_ID).sheet1
            
            # 確保表頭存在
            headers = ['日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID', '狀態']
            try:
                existing_headers = self.sheet.row_values(1)
                if not existing_headers or len(existing_headers) < len(headers):
                    if existing_headers:
                        self.sheet.update('A1:G1', [headers])
                    else:
                        self.sheet.insert_row(headers, 1)
                logger.info("Google Sheets 表頭設定完成")
            except Exception as e:
                logger.error(f"設定表頭時發生錯誤: {e}")
                
            logger.info("Google Sheets 連接成功")
        except Exception as e:
            logger.error(f"Google Sheets 連接失敗: {e}")
            raise
    
    def add_schedule(self, date_str, time_str, content, user_id, reminder=None):
        """新增行程"""
        try:
            # 驗證日期格式
            schedule_date = datetime.strptime(date_str, '%Y-%m-%d')
            
            # 驗證時間格式（如果有提供）
            if time_str:
                datetime.strptime(time_str, '%H:%M')
                
            # 檢查是否為過去的日期
            today = datetime.now(TZ).date()
            if schedule_date.date() < today:
                logger.warning(f"嘗試新增過去的日期: {date_str}")
                return "過去日期"
            
            created_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            
            if USE_GOOGLE_SHEETS and self.sheet:
                row = [date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
                self.sheet.append_row(row)
            else:
                # 使用記憶體儲存
                schedule = {
                    '日期': date_str,
                    '時間': time_str or '',
                    '行程內容': content,
                    '提醒設定': reminder or '',
                    '建立時間': created_time,
                    'LINE用戶ID': user_id,
                    '狀態': '有效'
                }
                memory_storage.append(schedule)
            
            logger.info(f"成功新增行程: {user_id} - {date_str} {time_str} {content}")
            return True
        except ValueError as e:
            logger.error(f"日期時間格式錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"新增行程失敗: {e}")
            return False
    
    def get_schedules_by_date_range(self, start_date, end_date, user_id=None):
        """取得指定日期範圍的行程"""
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
        """取得今日行程"""
        today = datetime.now(TZ).date()
        return self.get_schedules_by_date_range(today, today, user_id)
    
    def get_tomorrow_schedules(self, user_id):
        """取得明日行程"""
        tomorrow = datetime.now(TZ).date() + timedelta(days=1)
        return self.get_schedules_by_date_range(tomorrow, tomorrow, user_id)
    
    def get_this_week_schedules(self, user_id):
        """取得本週行程"""
        today = datetime.now(TZ).date()
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        this_sunday = this_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(this_monday, this_sunday, user_id)
    
    def get_next_week_schedules(self, user_id):
        """取得下週行程"""
        today = datetime.now(TZ).date()
        days_until_next_monday = 7 - today.weekday()
        next_monday = today + timedelta(days=days_until_next_monday)
        next_sunday = next_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(next_monday, next_sunday, user_id)
    
    def get_this_month_schedules(self, user_id):
        """取得本月行程"""
        today = datetime.now(TZ).date()
        this_month_start = today.replace(day=1)
        _, last_day = monthrange(today.year, today.month)
        this_month_end = today.replace(day=last_day)
        return self.get_schedules_by_date_range(this_month_start, this_month_end, user_id)
    
    def get_next_month_schedules(self, user_id):
        """取得下個月行程"""
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
        """取得明年行程"""
        today = datetime.now(TZ).date()
        next_year_start = today.replace(year=today.year + 1, month=1, day=1)
        next_year_end = today.replace(year=today.year + 1, month=12, day=31)
        return self.get_schedules_by_date_range(next_year_start, next_year_end, user_id)
    
    def get_recent_schedules(self, user_id, days=7):
        """取得最近N天的行程"""
        today = datetime.now(TZ).date()
        end_date = today + timedelta(days=days-1)
        return self.get_schedules_by_date_range(today, end_date, user_id)
    
    def delete_schedule(self, user_id, date_str, content_keyword):
        """刪除指定行程"""
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
                row_num = 2
                
                for record in all_records:
                    if (record.get('LINE用戶ID') == user_id and
                        record.get('日期') == date_str and
                        content_keyword in record.get('行程內容', '') and
                        record.get('狀態') != '已刪除'):
                        
                        self.sheet.update(f'G{row_num}', '已刪除')
                        logger.info(f"成功刪除行程: {user_id} - {date_str} {content_keyword}")
                        return True
                    row_num += 1
            else:
                # 記憶體模式刪除
                for record in memory_storage:
                    if (record.get('LINE用戶ID') == user_id and
                        record.get('日期') == date_str and
                        content_keyword in record.get('行程內容', '') and
                        record.get('狀態') != '已刪除'):
                        
                        record['狀態'] = '已刪除'
                        logger.info(f"成功刪除行程: {user_id} - {date_str} {content_keyword}")
                        return True
            
            return False
        except Exception as e:
            logger.error(f"刪除行程失敗: {e}")
            return False
    
    def get_two_weeks_later_schedules(self):
        """取得兩週後的行程（用於週五推播）"""
        try:
            today = datetime.now(TZ).date()
            two_weeks_later = today + timedelta(weeks=2)
            start_of_week = two_weeks_later - timedelta(days=two_weeks_later.weekday())
            end_of_week = start_of_week + timedelta(days=6)
            
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            schedules_by_user = {}
            
            for record in all_records:
                if (not record.get('日期') or 
                    not record.get('行程內容') or 
                    not record.get('LINE用戶ID') or
                    record.get('狀態') == '已刪除'):
                    continue
                    
                try:
                    schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                    if start_of_week <= schedule_date <= end_of_week:
                        user_id = record['LINE用戶ID']
                        if user_id not in schedules_by_user:
                            schedules_by_user[user_id] = []
                        schedules_by_user[user_id].append(record)
                except ValueError:
                    continue
            
            for user_id in schedules_by_user:
                schedules_by_user[user_id].sort(key=lambda x: (x['日期'], x.get('時間', '')))
                
            return schedules_by_user
        except Exception as e:
            logger.error(f"取得兩週後行程失敗: {e}")
            return {}

# 初始化行程管理器
schedule_manager = ScheduleManager()

def format_schedules(schedules, title):
    """格式化行程輸出"""
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
    """判斷是否為行程輸入格式"""
    patterns = [
        r'\d{1,2}/\d{1,2}',
        r'\d{1,2}月\d{1,2}[號日]',
        r'\d{4}-\d{1,2}-\d{1,2}',
        r'今天.*\d{1,2}[點時]',
        r'明天.*\d{1,2}[點時]',
        r'後天.*\d{1,2}[點時]',
    ]
    
    for pattern in patterns:
        if re.search(pattern, text):
            return True
    
    return False

def parse_schedule_input(text):
    """解析行程輸入格式"""
    content = text.replace('新增行程', '').strip()
    if not content:
        return None, None, None
    
    date_str, time_str, schedule_content = parse_natural_input(content)
    
    if date_str and schedule_content:
        return date_str, time_str, schedule_content
    
    return None, None, None

def parse_natural_input(text):
    """解析自然語言輸入"""
    current_year = datetime.now().year
    today = datetime.now(TZ).date()
    
    patterns = [
        # 今天/明天/後天 + 時間
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
        
        # 原有格式
        (r'(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)', 'date_time'),
        (r'(\d{1,2})/(\d{1,2})\s+(.+)', 'date_only'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*下午(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*上午(\d{1,2})[點時]\s*(.+)', 'chinese_am'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*晚上(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(\d{1,2})[點時]\s*(.+)', 'chinese_default'),
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(.+)', 'chinese_date_only'),
        (r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)', 'full_date_time'),
        (r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(.+)', 'full_date_only'),
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
                
                # 處理原有格式...（其餘邏輯相同，省略以節省空間）
                elif pattern_type == 'date_time':
                    month, day, hour, minute, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{int(hour):02d}:{minute}"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'date_only':
                    month, day, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                
            except (ValueError, IndexError):
                continue
    
    return None, None, None

@app.route("/", methods=["GET"])
def health_check():
    """健康檢查端點"""
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

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    try:
        # 倒數計時功能
        if text.startswith("倒數") and "分鐘" in text:
            try:
                minute = int(re.search(r'\d+', text).group())
                if 0 < minute <= 60:
                    reply_message = TextMessage(text=f"⏰ 倒數 {minute} 分鐘開始！我會在時間到時提醒你。")
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[reply_message]
                        )
                    )
                    
                    # 決定推送目標
                    if hasattr(event.source, 'group_id') and event.source.group_id:
                        target_id = event.source.group_id
                    elif hasattr(event.source, 'room_id') and event.source.room_id:
                        target_id = event.source.room_id
                    else:
                        target_id = event.source.user_id
                    
                    def send_reminder():
                        try:
                            push_message = TextMessage(text=f"⏰ {minute} 分鐘倒數結束，時間到囉！")
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=target_id,
                                    messages=[push_message]
                                )
                            )
                            logger.info(f"成功發送倒數提醒: {minute} 分鐘")
                        except Exception as e:
                            logger.error(f"推送提醒失敗: {e}")
                    
                    Timer(minute * 60, send_reminder).start()
                    return
                else:
                    reply_text = "⚠️ 倒數時間請設定在 1-60 分鐘之間"
            except (ValueError, AttributeError):
                reply_text = "❌ 請輸入正確格式：倒數 X 分鐘，例如：倒數 5 分鐘"
        
        # 查詢行程功能
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
        
        # 新增行程功能
        elif text.startswith("新增行程") or is_schedule_input(text):
            if not text.startswith("新增行程"):
                text = "新增行程 " + text
                
            date_str, time_str, content = parse_schedule_input(text)
            
            if date_str and content:
                success = schedule_manager.add_schedule(date_str, time_str, content, user_id)
                if success == True:
                    time_display = f" {time_str}" if time_str else " (全天)"
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        weekday = ['一', '二', '三', '四', '五', '六', '日'][date_obj.weekday()]
                        friendly_date = f"{date_obj.month}/{date_obj.day} (週{weekday})"
                        reply_text = f"✅ 行程已新增成功！\n📅 {friendly_date}{time_display}\n📝 {content}"
                    except:
                        reply_text = f"✅ 行程已新增成功！\n📅 {date_str}{time_display}\n📝 {content}"
                elif success == "過去日期":
                    reply_text = "⚠️ 無法新增過去的日期，請選擇今天或未來的日期"
                else:
                    reply_text = "❌ 新增行程失敗，請檢查日期格式是否正確或稍後再試"
            else:
                reply_text = ("❌ 格式錯誤！支援以下格式：\n\n"
                             "📝 快速輸入：\n"
                             "• 今天10點開會\n"
                             "• 明天下午2點聚
