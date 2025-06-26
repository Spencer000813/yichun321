import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort

# 嘗試使用 LINE Bot SDK v3，如果失敗則回退到 v2
try:
    from linebot.v3.webhook import WebhookHandler
    from linebot.v3.exceptions import InvalidSignatureError
    from linebot.v3.messaging import (
        Configuration, ApiClient, MessagingApi,
        ReplyMessageRequest, TextMessage, PushMessageRequest
    )
    from linebot.v3.webhooks import MessageEvent, TextMessageContent
    LINEBOT_SDK_VERSION = 3
    logger = logging.getLogger(__name__)
    logger.info("使用 LINE Bot SDK v3")
except ImportError:
    # 回退到 v2
    from linebot import LineBotApi, WebhookHandler
    from linebot.exceptions import InvalidSignatureError
    from linebot.models import MessageEvent, TextMessage, TextSendMessage
    LINEBOT_SDK_VERSION = 2
    logger = logging.getLogger(__name__)
    logger.info("回退到 LINE Bot SDK v2")
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

# 初始化 LINE Bot API（根據版本）
if LINEBOT_SDK_VERSION == 3:
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        api_client = ApiClient(configuration)
        line_bot_api = MessagingApi(api_client)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
    except Exception as e:
        logger.error(f"LINE Bot SDK v3 初始化失敗: {e}")
        # 如果 v3 初始化失敗，嘗試 v2
        LINEBOT_SDK_VERSION = 2
        
if LINEBOT_SDK_VERSION == 2:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
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
                logger.error(f"設定表頭時發生錯誤: {e}")
                
            logger.info("Google Sheets 連接成功")
            
            # 測試寫入權限
            try:
                test_row = len(self.sheet.get_all_values()) + 1
                logger.info(f"Sheet 目前有 {test_row - 1} 行資料")
            except Exception as e:
                logger.error(f"讀取 Sheet 資料時發生錯誤: {e}")
                
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
                try:
                    # 產生唯一 ID
                    schedule_id = f"S{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    
                    row = [schedule_id, date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
                    
                    # 使用 append_row 方法
                    self.sheet.append_row(row)
                    logger.info(f"成功寫入 Google Sheets: {schedule_id}")
                    
                    # 驗證寫入
                    try:
                        all_records = self.sheet.get_all_records()
                        latest_record = all_records[-1] if all_records else None
                        if latest_record and latest_record.get('ID') == schedule_id:
                            logger.info(f"驗證寫入成功: {schedule_id}")
                        else:
                            logger.warning(f"寫入驗證失敗: {schedule_id}")
                    except Exception as e:
                        logger.error(f"驗證寫入時發生錯誤: {e}")
                        
                except Exception as e:
                    logger.error(f"寫入 Google Sheets 失敗: {e}")
                    # 如果寫入失敗，回退到記憶體模式
                    schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    schedule = {
                        'ID': schedule_id,
                        '日期': date_str,
                        '時間': time_str or '',
                        '行程內容': content,
                        '提醒設定': reminder or '',
                        '建立時間': created_time,
                        'LINE用戶ID': user_id,
                        '狀態': '有效'
                    }
                    memory_storage.append(schedule)
                    logger.info(f"回退到記憶體模式儲存: {schedule_id}")
            else:
                # 記憶體模式
                schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                schedule = {
                    'ID': schedule_id,
                    '日期': date_str,
                    '時間': time_str or '',
                    '行程內容': content,
                    '提醒設定': reminder or '',
                    '建立時間': created_time,
                    'LINE用戶ID': user_id,
                    '狀態': '有效'
                }
                memory_storage.append(schedule)
            
            logger.info(f"成功新增行程: {user_id} - {date_str} {time_str} {content} (ID: {schedule_id})")
            return schedule_id  # 返回行程 ID
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
    
    def get_schedule_by_id(self, schedule_id, user_id=None):
        """根據 ID 查詢特定行程"""
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            for record in all_records:
                if (record.get('ID') == schedule_id and 
                    record.get('狀態') != '已刪除'):
                    
                    # 如果指定了 user_id，則檢查是否為該用戶的行程
                    if user_id and record.get('LINE用戶ID') != user_id:
                        return None
                    
                    return record
            
            return None
        except Exception as e:
            logger.error(f"查詢行程 ID 失敗: {e}")
            return None
    
    def get_user_schedules_with_id(self, user_id, limit=10):
        """取得用戶最近的行程（包含 ID）"""
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
            else:
                all_records = memory_storage
            
            user_schedules = []
            
            for record in all_records:
                if (record.get('LINE用戶ID') == user_id and 
                    record.get('狀態') != '已刪除'):
                    user_schedules.append(record)
            
            # 按建立時間排序，最新的在前
            user_schedules.sort(key=lambda x: x.get('建立時間', ''), reverse=True)
            
            return user_schedules[:limit]
        except Exception as e:
            logger.error(f"查詢用戶行程失敗: {e}")
            return []
    
    def get_recent_schedules(self, user_id, days=7):
        """取得最近N天的行程"""
        today = datetime.now(TZ).date()
        end_date = today + timedelta(days=days-1)
        return self.get_schedules_by_date_range(today, end_date, user_id)
    
    def delete_schedule_by_id(self, schedule_id, user_id):
        """根據 ID 刪除行程"""
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
                row_num = 2  # 從第二行開始（第一行是表頭）
                
                for record in all_records:
                    if (record.get('ID') == schedule_id and
                        record.get('LINE用戶ID') == user_id and
                        record.get('狀態') != '已刪除'):
                        
                        # 標記為已刪除
                        self.sheet.update(f'H{row_num}', '已刪除')
                        logger.info(f"成功刪除行程 ID: {schedule_id}")
                        return record
                    row_num += 1
            else:
                # 記憶體模式刪除
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
    def delete_schedule(self, user_id, date_str, content_keyword):
        """刪除指定行程（原有方法保留）"""
        try:
            if USE_GOOGLE_SHEETS and self.sheet:
                all_records = self.sheet.get_all_records()
                row_num = 2
                
                for record in all_records:
                    if (record.get('LINE用戶ID') == user_id and
                        record.get('日期') == date_str and
                        content_keyword in record.get('行程內容', '') and
                        record.get('狀態') != '已刪除'):
                        
                        self.sheet.update(f'H{row_num}', '已刪除')
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
                    
                    # 決定推送目標
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
                                line_bot_api.push_message(
                                    PushMessageRequest(
                                        to=target_id,
                                        messages=[push_message]
                                    )
                                )
                            else:
                                line_bot_api.push_message(target_id, TextSendMessage(text=reminder_text))
                            logger.info(f"成功發送倒數提醒: {minute} 分鐘")
                        except Exception as e:
                            logger.error(f"推送提醒失敗: {e}")
                    
                    Timer(minute * 60, send_reminder).start()
                    
                    # 立即回覆確認訊息
                    if LINEBOT_SDK_VERSION == 3:
                        reply_message = TextMessage(text=reply_text)
                        line_bot_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[reply_message]
                            )
                        )
                    else:
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(text=reply_text)
                        )
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
        
        # 查詢行程 ID
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
        
        # 我的行程 ID 列表
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
                    # 成功新增，返回了行程 ID
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
                             "• 7/14 聚餐\n"
                             "• 6月30號 下午2點 盤點\n"
                             "• 12月25號 聖誕節\n\n"
                             "📝 完整格式：\n"
                             "• 新增行程 2024-12-25 09:30 會議\n"
                             "• 新增行程 2024-12-25 聖誕節")
        
        # 刪除行程功能
        elif text.startswith("刪除行程"):
            content = text.replace('刪除行程', '').strip()
            if content:
                parts = content.split(' ', 1)
                if len(parts) >= 2:
                    date_part = parts[0]
                    keyword = parts[1]
                    
                    try:
                        if '/' in date_part:
                            month, day = date_part.split('/')
                            current_year = datetime.now().year
                            date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                        elif '月' in date_part and ('號' in date_part or '日' in date_part):
                            match = re.match(r'(\d{1,2})月(\d{1,2})[號日]', date_part)
                            if match:
                                month, day = match.groups()
                                current_year = datetime.now().year
                                date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                            else:
                                date_str = date_part
                        else:
                            date_str = date_part
                        
                        success = schedule_manager.delete_schedule(user_id, date_str, keyword)
                        if success:
                            reply_text = f"✅ 已成功刪除包含「{keyword}」的行程"
                        else:
                            reply_text = f"❌ 找不到符合條件的行程：{date_str} {keyword}"
                    except:
                        reply_text = "❌ 日期格式錯誤，請使用：刪除行程 7/14 關鍵字"
                else:
                    reply_text = "❌ 格式錯誤，請使用：刪除行程 7/14 關鍵字"
            else:
                reply_text = "❌ 請輸入要刪除的行程，格式：刪除行程 7/14 關鍵字"
        
        # 根據 ID 刪除行程
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
        
        # 功能菜單
        elif text in ["功能", "menu", "選單", "菜單"]:
            reply_text = ("🎯 功能選單\n\n"
                         "請選擇您需要的功能：\n\n"
                         "📝 新增行程 → 輸入「新增說明」\n"
                         "🔍 查詢行程 → 輸入「查詢說明」\n"
                         "🗑️ 刪除行程 → 輸入「刪除說明」\n"
                         "🆔 行程ID管理 → 輸入「ID說明」\n"
                         "⏰ 倒數計時 → 輸入「倒數說明」\n"
                         "🔧 系統功能 → 輸入「系統說明」\n"
                         "📖 完整說明 → 輸入「完整說明」\n\n"
                         "💡 提示：您也可以直接輸入行程資訊，例如：\n"
                         "「今天10點開會」、「7/14 聚餐」")「今天10點開會」、「7/14 聚餐」")
        
        # 新增行程說明
        elif text in ["新增說明", "新增幫助", "新增功能"]:
            reply_text = ("📝 新增行程功能說明\n\n"
                         "🌟 支援多種自然語言格式：\n\n"
                         "📅 相對日期格式：\n"
                         "• 今天10點開會\n"
                         "• 明天下午2點聚餐\n"
                         "• 後天上午9點會議\n"
                         "• 今天晚上8點聚會\n\n"
                         "📅 日期/月份格式：\n"
                         "• 7/14 10:00 開會\n"
                         "• 7/14 聚餐（全天）\n"
                         "• 12/25 聖誕節\n\n"
                         "📅 中文日期格式：\n"
                         "• 6月30號 下午2點 盤點\n"
                         "• 12月25號 聖誕節\n"
                         "• 7月4號 上午10點 會議\n\n"
                         "📅 完整格式：\n"
                         "• 新增行程 2024-12-25 09:30 會議\n"
                         "• 新增行程 2024-12-25 聖誕節\n\n"
                         "⚠️ 注意事項：\n"
                         "• 不能新增過去的日期\n"
                         "• 時間格式：24小時制（如：14點 = 下午2點）\n"
                         "• 沒有指定時間的行程視為全天行程")
        
        # 查詢行程說明
        elif text in ["查詢說明", "查詢幫助", "查詢功能"]:
            reply_text = ("🔍 查詢行程功能說明\n\n"
                         "📊 可查詢的時間範圍：\n\n"
                         "📅 今日行程 → 查看今天的所有行程\n"
                         "📅 明日行程 → 查看明天的所有行程\n\n"
                         "📅 本週行程 → 查看這週（週一到週日）的行程\n"
                         "📅 下週行程 → 查看下週的行程\n\n"
                         "📅 本月行程 → 查看這個月的行程\n"
                         "📅 下個月行程 → 查看下個月的行程\n\n"
                         "📅 明年行程 → 查看明年的行程\n"
                         "📅 近期行程 → 查看未來7天的行程\n\n"
                         "📋 顯示格式：\n"
                         "• 按日期和時間自動排序\n"
                         "• 顯示星期幾（週一、週二...）\n"
                         "• 全天行程會標示「(全天)」\n"
                         "• 如果沒有行程會顯示「目前沒有安排任何行程」")
        
        # ID 管理說明
        elif text in ["ID說明", "id說明", "ID功能", "id功能"]:
            reply_text = ("🆔 行程ID管理功能說明\n\n"
                         "🌟 什麼是行程ID？\n"
                         "每筆行程都有唯一的識別碼，方便精確管理\n\n"
                         "📝 ID格式說明：\n"
                         "• S開頭：Google Sheets儲存的行程\n"
                         "• M開頭：記憶體模式儲存的行程\n"
                         "• 格式：S20240626120000001\n\n"
                         "🔍 查詢功能：\n"
                         "• 我的行程 - 列出所有行程及其ID\n"
                         "• 行程列表 - 同上\n"
                         "• 行程ID - 同上\n"
                         "• 查詢ID [ID號碼] - 查看特定行程詳細資訊\n\n"
                         "🗑️ 刪除功能：\n"
                         "• 刪除ID [ID號碼] - 精確刪除特定行程\n\n"
                         "🎯 使用範例：\n"
                         "• 我的行程\n"
                         "• 查詢ID S20240626120000001\n"
                         "• 刪除ID S20240626120000001\n\n"
                         "✅ 優點：\n"
                         "• 精確管理，不會誤刪\n"
                         "• 可查看詳細建立時間\n"
                         "• 支援批量管理")
        # 刪除行程說明
        elif text in ["刪除說明", "刪除幫助", "刪除功能"]:
            reply_text = ("🗑️ 刪除行程功能說明\n\n"
                         "📝 方法一：關鍵字刪除\n"
                         "格式：刪除行程 [日期] [關鍵字]\n\n"
                         "🎯 使用範例：\n"
                         "• 刪除行程 7/14 開會\n"
                         "• 刪除行程 12/25 聚餐\n"
                         "• 刪除行程 6月30號 盤點\n\n"
                         "📝 方法二：ID精確刪除（推薦）\n"
                         "格式：刪除ID [ID號碼]\n\n"
                         "🎯 使用範例：\n"
                         "• 刪除ID S20240626120000001\n\n"
                         "🔍 搜尋規則：\n"
                         "• 關鍵字刪除：搜尋指定日期包含關鍵字的行程\n"
                         "• ID刪除：精確刪除特定行程\n"
                         "• 只會刪除您自己建立的行程\n"
                         "• 刪除後無法復原，請謹慎操作\n\n"
                         "✅ 成功刪除會顯示確認訊息\n"
                         "❌ 找不到符合條件的行程會顯示錯誤訊息\n\n"
                         "💡 小技巧：\n"
                         "• 使用「我的行程」查看所有行程ID\n"
                         "• ID刪除更精確，避免誤刪\n"
                         "• 關鍵字不需要完全相符，部分相符即可")
        
        # 倒數計時說明
        elif text in ["倒數說明", "倒數幫助", "倒數功能"]:
            reply_text = ("⏰ 倒數計時功能說明\n\n"
                         "📝 使用格式：\n"
                         "倒數 [數字] 分鐘\n\n"
                         "🎯 使用範例：\n"
                         "• 倒數 5 分鐘\n"
                         "• 倒數 15 分鐘\n"
                         "• 倒數 30 分鐘\n"
                         "• 倒數 60 分鐘\n\n"
                         "⚙️ 功能特色：\n"
                         "• 支援 1-60 分鐘的倒數計時\n"
                         "• 開始倒數時會立即回覆確認訊息\n"
                         "• 時間到時自動推送提醒訊息\n"
                         "• 在群組中使用會推送到群組\n"
                         "• 在私聊中使用會推送給個人\n\n"
                         "🔔 提醒方式：\n"
                         "時間到時會收到：\n"
                         "「⏰ X 分鐘倒數結束，時間到囉！」\n\n"
                         "⚠️ 注意事項：\n"
                         "• 時間範圍：1-60分鐘\n"
                         "• 超出範圍會顯示錯誤訊息")
        
        # 系統功能說明
        elif text in ["系統說明", "系統幫助", "系統功能"]:
            reply_text = ("🔧 系統功能說明\n\n"
                         "📊 狀態查詢：\n"
                         "輸入「狀態」查看系統運行狀態\n"
                         "• 資料儲存模式（Google Sheets 或記憶體模式）\n"
                         "• 系統中的總行程數量\n"
                         "• 您個人的行程數量\n"
                         "• 目前系統時間\n"
                         "• 系統時區設定\n\n"
                         "🔔 自動提醒功能：\n"
                         "• 每週五早上10點自動推播\n"
                         "• 提醒兩週後（下下週）的行程\n"
                         "• 只推播給有行程的用戶\n"
                         "• 按日期和時間排序顯示\n\n"
                         "💾 資料儲存：\n"
                         "• Google Sheets模式：資料永久保存\n"
                         "• 記憶體模式：重啟後資料清除\n"
                         "• 支援軟刪除，保留歷史記錄\n\n"
                         "🌏 時區設定：\n"
                         "• 預設使用台北時區 (Asia/Taipei)\n"
                         "• 所有時間計算基於台北時間\n\n"
                         "🔄 每日維護：\n"
                         "• 每日凌晨2點執行清理任務\n"
                         "• 自動清理過期的已刪除行程")
        
        # 完整說明
        elif text in ["完整說明", "完整幫助", "使用手冊", "說明書"]:
            reply_text = ("📖 LINE Bot 行程管理系統 - 完整使用手冊\n\n"
                         "🤖 系統介紹：\n"
                         "這是一個智能行程管理機器人，支援自然語言輸入、ID管理、自動提醒等功能。\n\n"
                         "🎯 核心功能：\n"
                         "1️⃣ 智能行程新增 - 支援多種自然語言格式\n"
                         "2️⃣ 靈活行程查詢 - 可查詢不同時間範圍\n"
                         "3️⃣ 便捷行程刪除 - 關鍵字+ID雙重刪除方式\n"
                         "4️⃣ 行程ID管理 - 精確管理每筆行程\n"
                         "5️⃣ 實用倒數計時 - 1-60分鐘倒數提醒\n"
                         "6️⃣ 自動行程提醒 - 週五推播兩週後行程\n\n"
                         "📝 快速開始：\n"
                         "• 新增行程：直接輸入「明天10點開會」\n"
                         "• 查詢行程：輸入「今日行程」\n"
                         "• 管理行程：輸入「我的行程」查看ID\n"
                         "• 倒數計時：輸入「倒數 5 分鐘」\n"
                         "• 查看功能：輸入「功能」\n\n"
                         "🔧 系統指令：\n"
                         "• 功能 - 顯示功能選單\n"
                         "• 狀態 - 查看系統狀態\n"
                         "• 我的行程 - 查看所有行程及ID\n"
                         "• 查詢ID [ID] - 查看特定行程詳情\n"
                         "• 刪除ID [ID] - 精確刪除行程\n"
                         "• 各功能說明 - 如「新增說明」等\n\n"
                         "💡 使用技巧：\n"
                         "• 支援繁體中文自然語言輸入\n"
                         "• 可以省略「新增行程」直接輸入行程\n"
                         "• 時間可用上午/下午/晚上等中文表達\n"
                         "• 每筆行程都有唯一ID便於管理\n"
                         "• 系統會自動判斷輸入格式\n\n"
                         "❓ 需要協助？\n"
                         "隨時輸入「功能」查看功能選單，或輸入具體功能的說明指令獲取詳細幫助。")
        
        # 簡短幫助訊息
        elif text in ["幫助", "help", "使用說明", "?"]:
            reply_text = ("🤖 LINE Bot 行程管理系統\n\n"
                         "🎯 快速指令：\n"
                         "• 功能 - 顯示完整功能選單\n"
                         "• 我的行程 - 查看所有行程及ID\n"
                         "• 完整說明 - 詳細使用手冊\n"
                         "• 狀態 - 查看系統狀態\n\n"
                         "⚡ 快速使用：\n"
                         "• 明天10點開會 - 新增行程\n"
                         "• 今日行程 - 查詢今天行程\n"
                         "• 查詢ID S123... - 查看特定行程\n"
                         "• 刪除ID S123... - 刪除特定行程\n"
                         "• 倒數 5 分鐘 - 開始倒數計時\n\n"
                         "💡 輸入「功能」查看所有功能選單")
        
        # 系統狀態查詢
        elif text in ["狀態", "系統狀態", "status"]:
            try:
                if USE_GOOGLE_SHEETS and schedule_manager.sheet:
                    test_records = schedule_manager.sheet.get_all_records()
                    sheets_status = "✅ 正常"
                    total_records = len([r for r in test_records if r.get('行程內容')])
                    user_records = len([r for r in test_records if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
                else:
                    sheets_status = "📱 記憶體模式"
                    total_records = len([r for r in memory_storage if r.get('行程內容')])
                    user_records = len([r for r in memory_storage if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
            except:
                sheets_status = "❌ 異常"
                total_records = 0
                user_records = 0
            
            reply_text = (f"🔧 系統狀態報告\n\n"
                         f"📊 資料儲存: {sheets_status}\n"
                         f"📈 總行程數: {total_records}\n"
                         f"👤 您的行程數: {user_records}\n"
                         f"🕐 系統時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                         f"🌏 時區: Asia/Taipei")
        
        # 倒數計時格式錯誤提醒
        elif text.startswith("倒數"):
            reply_text = "❌ 請輸入正確格式：倒數 X 分鐘，例如：倒數 5 分鐘（1-60分鐘）"
        
        # 未知指令
        else:
            reply_text = ("🤔 我不太理解您的指令\n\n"
                         "請輸入「幫助」查看使用說明，或直接輸入行程資訊\n"
                         "例如：今天10點開會、7/14 聚餐")
        
        # 發送回覆（兼容兩個版本）
        if LINEBOT_SDK_VERSION == 3:
            reply_message = TextMessage(text=reply_text)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[reply_message]
                )
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )
    
    except Exception as e:
        error_msg = f"處理訊息時發生錯誤: {str(e)}"
        logger.error(error_msg)
        try:
            error_text = "系統發生異常，請稍後再試或聯繫管理員"
            if LINEBOT_SDK_VERSION == 3:
                error_reply = TextMessage(text=error_text)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[error_reply]
                    )
                )
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=error_text)
                )
        except:
            pass

def friday_reminder():
    """週五早上10點推播兩週後行程"""
    try:
        schedules_by_user = schedule_manager.get_two_weeks_later_schedules()
        
        if not schedules_by_user:
            logger.info("週五提醒：沒有用戶有兩週後的行程")
            return
        
        for user_id, schedules in schedules_by_user.items():
            if schedules:
                message = "🔔 兩週後行程提醒\n\n"
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
                
                try:
                    message_text = message.strip()
                    if LINEBOT_SDK_VERSION == 3:
                        push_message = TextMessage(text=message_text)
                        line_bot_api.push_message(
                            PushMessageRequest(
                                to=user_id,
                                messages=[push_message]
                            )
                        )
                    else:
                        line_bot_api.push_message(user_id, TextSendMessage(text=message_text))
                    logger.info(f"成功推播週五提醒給用戶: {user_id}")
                except Exception as e:
                    logger.error(f"推播失敗 {user_id}: {e}")
        
        logger.info(f"週五提醒執行完成，共推播給 {len(schedules_by_user)} 位用戶")
    except Exception as e:
        logger.error(f"週五提醒執行失敗: {e}")

def daily_cleanup():
    """每日清理過期的已刪除行程"""
    try:
        logger.info("每日清理任務執行")
    except Exception as e:
        logger.error(f"每日清理任務執行失敗: {e}")

# 設定排程器
scheduler = BackgroundScheduler(timezone=TZ)

# 週五早上10點推播兩週後行程
scheduler.add_job(
    friday_reminder,
    'cron',
    day_of_week='fri',
    hour=10,
    minute=0,
    id='friday_reminder'
)

# 每日凌晨2點執行清理任務
scheduler.add_job(
    daily_cleanup,
    'cron',
    hour=2,
    minute=0,
    id='daily_cleanup'
)

# 錯誤處理
@app.errorhandler(404)
def not_found(error):
    return "Not Found", 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return "Internal Server Error", 500

# 確保程式結束時正確關閉排程器
def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("排程器已關閉")

atexit.register(shutdown_scheduler)

if __name__ == "__main__":
    try:
        # 啟動排程器
        scheduler.start()
        logger.info("排程器已啟動")
        
        # 測試連接
        if USE_GOOGLE_SHEETS:
            try:
                test_records = schedule_manager.sheet.get_all_records()
                logger.info(f"Google Sheets 連接測試成功，共 {len(test_records)} 筆記錄")
            except Exception as e:
                logger.error(f"Google Sheets 連接測試失敗: {e}")
        else:
            logger.info("使用記憶體模式運行")
        
        # 啟動 Flask 應用
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
