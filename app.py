import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
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
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET 環境變數")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 設定
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # JSON 格式的服務帳戶金鑰
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  # Google Sheets 的 ID

if not GOOGLE_CREDENTIALS or not SPREADSHEET_ID:
    raise ValueError("請設定 GOOGLE_CREDENTIALS 和 SPREADSHEET_ID 環境變數")

# 時區設定
TZ = pytz.timezone('Asia/Taipei')

class ScheduleManager:
    def __init__(self):
        self.gc = None
        self.sheet = None
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
                    # 如果表頭不完整，重新設定
                    if existing_headers:
                        # 保留現有資料，只更新表頭
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
        """新增行程到 Google Sheets"""
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
            row = [date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
            self.sheet.append_row(row)
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
            all_records = self.sheet.get_all_records()
            schedules = []
            
            for record in all_records:
                # 跳過空白行或無效資料
                if not record.get('日期') or not record.get('行程內容'):
                    continue
                    
                # 檢查狀態（避免顯示已刪除的行程）
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
        # 計算本週一
        days_since_monday = today.weekday()
        this_monday = today - timedelta(days=days_since_monday)
        this_sunday = this_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(this_monday, this_sunday, user_id)
    
    def get_next_week_schedules(self, user_id):
        """取得下週行程"""
        today = datetime.now(TZ).date()
        # 計算下週一
        days_until_next_monday = 7 - today.weekday()
        next_monday = today + timedelta(days=days_until_next_monday)
        next_sunday = next_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(next_monday, next_sunday, user_id)
    
    def get_this_month_schedules(self, user_id):
        """取得本月行程"""
        today = datetime.now(TZ).date()
        this_month_start = today.replace(day=1)
        # 計算本月最後一天
        _, last_day = monthrange(today.year, today.month)
        this_month_end = today.replace(day=last_day)
        return self.get_schedules_by_date_range(this_month_start, this_month_end, user_id)
    
    def get_next_month_schedules(self, user_id):
        """取得下個月行程"""
        today = datetime.now(TZ).date()
        
        # 計算下個月第一天
        if today.month == 12:
            next_month_start = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month_start = today.replace(month=today.month + 1, day=1)
        
        # 計算下個月最後一天
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
            all_records = self.sheet.get_all_records()
            row_num = 2  # 從第二行開始（第一行是表頭）
            
            for record in all_records:
                if (record.get('LINE用戶ID') == user_id and
                    record.get('日期') == date_str and
                    content_keyword in record.get('行程內容', '') and
                    record.get('狀態') != '已刪除'):
                    
                    # 標記為已刪除而不是真的刪除
                    self.sheet.update(f'G{row_num}', '已刪除')
                    logger.info(f"成功刪除行程: {user_id} - {date_str} {content_keyword}")
                    return True
                row_num += 1
            
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
            
            all_records = self.sheet.get_all_records()
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
            
            # 為每個用戶的行程排序
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
        
        # 如果是新的日期，顯示日期分隔
        if date != current_date:
            if current_date is not None:
                message += "\n"
            current_date = date
            
            # 轉換日期格式為更友善的顯示
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
    # 檢查是否符合自然語言行程輸入格式
    patterns = [
        r'\d{1,2}/\d{1,2}',  # 7/14 格式
        r'\d{1,2}月\d{1,2}[號日]',  # 6月30號 格式
        r'\d{4}-\d{1,2}-\d{1,2}',  # 2024-07-14 格式
        r'今天.*\d{1,2}[點時]',  # 今天10點
        r'明天.*\d{1,2}[點時]',  # 明天2點
        r'後天.*\d{1,2}[點時]',  # 後天3點
    ]
    
    for pattern in patterns:
        if re.search(pattern, text):
            return True
    
    return False

def parse_schedule_input(text):
    """解析行程輸入格式"""
    # 移除「新增行程」前綴
    content = text.replace('新增行程', '').strip()
    if not content:
        return None, None, None
    
    # 分析輸入內容
    date_str, time_str, schedule_content = parse_natural_input(content)
    
    if date_str and schedule_content:
        return date_str, time_str, schedule_content
    
    return None, None, None

def parse_natural_input(text):
    """解析自然語言輸入"""
    current_year = datetime.now().year
    today = datetime.now(TZ).date()
    
    # 正規表達式模式
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
        
        # 原有的格式
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
                # 處理今天/明天/後天
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
                
                # 處理原有格式
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
                
                elif pattern_type == 'chinese_pm':
                    month, day, hour, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    hour = int(hour)
                    if hour < 12:
                        hour += 12
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'chinese_am':
                    month, day, hour, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    hour = int(hour)
                    if hour == 12:
                        hour = 0
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'chinese_default':
                    month, day, hour, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    hour = int(hour)
                    if hour > 24:
                        continue
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'chinese_date_only':
                    month, day, content = match.groups()
                    date_obj = datetime(current_year, int(month), int(day))
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                
                elif pattern_type == 'full_date_time':
                    year, month, day, hour, minute, content = match.groups()
                    date_obj = datetime(int(year), int(month), int(day))
                    date_str = f"{year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{int(hour):02d}:{minute}"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'full_date_only':
                    year, month, day, content = match.groups()
                    date_obj = datetime(int(year), int(month), int(day))
                    date_str = f"{year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                
            except (ValueError, IndexError):
                continue
    
    return None, None, None

@app.route("/", methods=["GET"])
def health_check():
    """健康檢查端點"""
    return "LINE Bot 行程管理系統運行中", 200

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    try:
        # 倒數計時功能
        if text.startswith("倒數") and "分鐘" in text:
            try:
                minute = int(re.search(r'\d+', text).group())
                if 0 < minute <= 60:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=f"⏰ 倒數 {minute} 分鐘開始！我會在時間到時提醒你。")
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
                            line_bot_api.push_message(
                                target_id,
                                TextSendMessage(text=f"⏰ {minute} 分鐘倒數結束，時間到囉！")
                            )
                            logger.info(f"成功發送倒數提醒: {minute} 分鐘")
                        except LineBotApiError as e:
                            logger.error(f"推送提醒失敗: {e}")
                        except Exception as e:
                            logger.error(f"推送提醒時發生未知錯誤: {e}")
                    
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
        
        # 新增行程功能（支援多種格式）
        elif text.startswith("新增行程") or is_schedule_input(text):
            # 如果不是以「新增行程」開頭，自動加上前綴
            if not text.startswith("新增行程"):
                text = "新增行程 " + text
                
            date_str, time_str, content = parse_schedule_input(text)
            
            if date_str and content:
                success = schedule_manager.add_schedule(date_str, time_str, content, user_id)
                if success == True:
                    time_display = f" {time_str}" if time_str else " (全天)"
                    # 轉換日期為更友善的格式
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
                # 嘗試解析日期和關鍵字
                parts = content.split(' ', 1)
                if len(parts) >= 2:
                    date_part = parts[0]
                    keyword = parts[1]
                    
                    # 嘗試轉換日期格式
                    try:
                        if '/' in date_part:
                            month, day = date_part.split('/')
                            current_year = datetime.now().year
                            date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                        elif '月' in date_part and ('號' in date_part or '日' in date_part):
                            # 處理中文日期格式
                            import re
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
        
        # 幫助訊息
        elif text in ["幫助", "help", "使用說明", "功能"]:
            reply_text = ("🤖 LINE Bot 行程管理系統\n\n"
                         "📝 新增行程（支援多種格式）：\n"
                         "• 今天10點開會\n"
                         "• 明天下午2點聚餐\n"
                         "• 後天上午9點會議\n"
                         "• 7/14 10:00 開會\n"
                         "• 7/14 聚餐\n"
                         "• 6月30號 下午2點 盤點\n"
                         "• 12月25號 聖誕節\n"
                         "• 新增行程 2024-12-25 09:30 會議\n\n"
                         "🔍 查詢行程：\n"
                         "• 今日行程\n"
                         "• 明日行程\n"
                         "• 本週行程 / 下週行程\n"
                         "• 本月行程 / 下個月行程\n"
                         "• 明年行程\n"
                         "• 近期行程\n\n"
                         "🗑️ 刪除行程：\n"
                         "• 刪除行程 7/14 關鍵字\n\n"
                         "⏰ 倒數計時：\n"
                         "• 倒數 5 分鐘\n\n"
                         "📢 系統會在每週五早上10點推播兩週後的行程提醒")
        
        # 系統狀態查詢
        elif text in ["狀態", "系統狀態", "status"]:
            try:
                # 測試 Google Sheets 連接
                test_records = schedule_manager.sheet.get_all_records()
                sheets_status = "✅ 正常"
                total_records = len([r for r in test_records if r.get('行程內容')])
                user_records = len([r for r in test_records if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
            except:
                sheets_status = "❌ 異常"
                total_records = 0
                user_records = 0
            
            reply_text = (f"🔧 系統狀態報告\n\n"
                         f"📊 Google Sheets: {sheets_status}\n"
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
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    
    except LineBotApiError as e:
        error_msg = f"LINE API 錯誤: {str(e)}"
        logger.error(error_msg)
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="抱歉，系統暫時無法回應，請稍後再試")
            )
        except:
            pass
    except Exception as e:
        error_msg = f"處理訊息時發生錯誤: {str(e)}"
        logger.error(error_msg)
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="系統發生異常，請稍後再試或聯繫管理員")
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
                    
                    # 如果是新的日期，顯示日期分隔
                    if date != current_date:
                        if current_date is not None:
                            message += "\n"
                        current_date = date
                        
                        # 轉換日期格式
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
                    line_bot_api.push_message(user_id, TextSendMessage(text=message.strip()))
                    logger.info(f"成功推播週五提醒給用戶: {user_id}")
                except LineBotApiError as e:
                    logger.error(f"推播失敗 {user_id}: {e}")
                except Exception as e:
                    logger.error(f"推播時發生未知錯誤 {user_id}: {e}")
        
        logger.info(f"週五提醒執行完成，共推播給 {len(schedules_by_user)} 位用戶")
    except Exception as e:
        logger.error(f"週五提醒執行失敗: {e}")

def daily_cleanup():
    """每日清理過期的已刪除行程"""
    try:
        # 這裡可以加入清理邏輯，例如刪除一個月前的已刪除行程
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
        
        # 測試 Google Sheets 連接
        try:
            test_records = schedule_manager.sheet.get_all_records()
            logger.info(f"Google Sheets 連接測試成功，共 {len(test_records)} 筆記錄")
        except Exception as e:
            logger.error(f"Google Sheets 連接測試失敗: {e}")
        
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
