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
            logger.info("開始設定 Google Sheets 連接...")
            
            # 解析憑證
            credentials_dict = json.loads(GOOGLE_CREDENTIALS)
            logger.info("成功解析 Google 憑證")
            
            # 建立憑證物件
            creds = Credentials.from_service_account_info(
                credentials_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            logger.info("成功建立 Google 憑證物件")
            
            # 授權並連接
            self.gc = gspread.authorize(creds)
            logger.info("成功授權 Google Sheets")
            
            # 開啟試算表
            self.sheet = self.gc.open_by_key(SPREADSHEET_ID).提醒
            logger.info(f"成功開啟試算表: {SPREADSHEET_ID}")
            
            # 測試讀取權限
            try:
                test_data = self.sheet.get_all_values()
                logger.info(f"測試讀取成功，目前有 {len(test_data)} 行資料")
            except Exception as e:
                logger.error(f"測試讀取失敗: {e}")
                raise
            
            # 確保表頭存在
            headers = ['ID', '日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID', '狀態']
            try:
                existing_headers = self.sheet.row_values(1)
                logger.info(f"現有表頭: {existing_headers}")
                
                if not existing_headers or len(existing_headers) < len(headers):
                    logger.info("需要設定表頭")
                    if existing_headers:
                        self.sheet.update('A1:H1', [headers])
                        logger.info("更新表頭完成")
                    else:
                        self.sheet.insert_row(headers, 1)
                        logger.info("插入表頭完成")
                else:
                    logger.info("表頭已存在且正確")
                    
            except Exception as e:
                logger.error(f"設定表頭時發生錯誤: {e}")
                # 不要因為表頭問題而中斷連接
                pass
                
            # 測試寫入權限
            try:
                test_id = f"TEST{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
                test_row = [test_id, '2099-12-31', '23:59', '測試行程', '', datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'), 'TEST_USER', '測試']
                self.sheet.append_row(test_row)
                logger.info("測試寫入成功")
                
                # 立即刪除測試資料
                try:
                    all_values = self.sheet.get_all_values()
                    for i, row in enumerate(all_values):
                        if len(row) > 0 and row[0] == test_id:
                            self.sheet.delete_rows(i + 1)
                            logger.info("測試資料已清除")
                            break
                except Exception as del_e:
                    logger.warning(f"清除測試資料失敗: {del_e}")
                    
            except Exception as e:
                logger.error(f"測試寫入失敗: {e}")
                raise
                
            logger.info("Google Sheets 連接設定完成")
            
        except Exception as e:
            logger.error(f"Google Sheets 連接失敗: {e}")
            raise
    
    def add_schedule(self, date_str, time_str, content, user_id, reminder=None):
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
            
            # 優先嘗試寫入 Google Sheets
            if USE_GOOGLE_SHEETS and self.sheet:
                try:
                    # 產生唯一 ID
                    schedule_id = f"S{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    
                    # 準備寫入資料
                    row = [schedule_id, date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
                    
                    # 寫入 Google Sheets
                    logger.info(f"正在寫入 Google Sheets: {schedule_id}")
                    self.sheet.append_row(row)
                    logger.info(f"成功寫入 Google Sheets: {schedule_id}")
                    
                    # 立即驗證寫入是否成功
                    import time
                    time.sleep(1)  # 等待1秒讓 Google Sheets 同步
                    
                    try:
                        # 檢查最後幾行是否包含我們剛寫入的資料
                        all_values = self.sheet.get_all_values()
                        if len(all_values) > 1:  # 確保有資料行
                            last_row = all_values[-1]
                            if len(last_row) > 0 and last_row[0] == schedule_id:
                                logger.info(f"驗證寫入成功: {schedule_id}")
                                return schedule_id
                            else:
                                logger.warning(f"寫入驗證失敗，最後一行: {last_row}")
                                raise Exception("寫入驗證失敗")
                        else:
                            logger.warning("無法驗證寫入，表格可能為空")
                            raise Exception("無法驗證寫入")
                    except Exception as verify_e:
                        logger.error(f"驗證寫入時發生錯誤: {verify_e}")
                        raise Exception(f"寫入驗證失敗: {verify_e}")
                        
                except Exception as sheets_e:
                    logger.error(f"寫入 Google Sheets 失敗: {sheets_e}")
                    logger.info("回退到記憶體模式儲存")
                    
                    # 回退到記憶體模式
                    schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                    schedule = {
                        'ID': schedule_id, '日期': date_str, '時間': time_str or '', '行程內容': content,
                        '提醒設定': reminder or '', '建立時間': created_time, 'LINE用戶ID': user_id, '狀態': '有效'
                    }
                    memory_storage.append(schedule)
                    logger.info(f"成功儲存到記憶體: {schedule_id}")
                    return schedule_id
            else:
                # 直接使用記憶體模式
                schedule_id = f"M{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
                schedule = {
                    'ID': schedule_id, '日期': date_str, '時間': time_str or '', '行程內容': content,
                    '提醒設定': reminder or '', '建立時間': created_time, 'LINE用戶ID': user_id, '狀態': '有效'
                }
                memory_storage.append(schedule)
                logger.info(f"記憶體模式儲存: {schedule_id}")
                return schedule_id
            
        except ValueError as e:
            logger.error(f"日期時間格式錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"新增行程失敗: {e}")
            return False
    
    def get_schedules_by_date_range(self, start_date, end_date, user_id=None):
        try:
            logger.info(f"查詢行程範圍: {start_date} 到 {end_date}, 用戶: {user_id}")
            
            # 優先從 Google Sheets 讀取
            if USE_GOOGLE_SHEETS and self.sheet:
                try:
                    logger.info("從 Google Sheets 讀取行程...")
                    all_records = self.sheet.get_all_records()
                    logger.info(f"從 Google Sheets 讀取到 {len(all_records)} 筆記錄")
                    
                    # 詳細記錄前幾筆資料以供偵錯
                    if all_records:
                        logger.info(f"前3筆記錄範例: {all_records[:3]}")
                    
                except Exception as e:
                    logger.error(f"從 Google Sheets 讀取失敗: {e}")
                    logger.info("回退到記憶體模式讀取")
                    all_records = memory_storage
            else:
                logger.info("使用記憶體模式讀取")
                all_records = memory_storage
            
            schedules = []
            processed_count = 0
            
            for record in all_records:
                processed_count += 1
                
                # 基本資料檢查
                if not record.get('日期') or not record.get('行程內容'):
                    logger.debug(f"跳過空白記錄 {processed_count}: {record}")
                    continue
                
                # 狀態檢查
                if record.get('狀態') == '已刪除':
                    logger.debug(f"跳過已刪除記錄 {processed_count}: {record.get('ID')}")
                    continue
                
                # 用戶檢查
                if user_id and record.get('LINE用戶ID') != user_id:
                    logger.debug(f"跳過其他用戶記錄 {processed_count}: {record.get('LINE用戶ID')} != {user_id}")
                    continue
                
                # 日期範圍檢查
                try:
                    schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                    if start_date <= schedule_date <= end_date:
                        schedules.append(record)
                        logger.debug(f"符合條件的記錄: {record.get('ID')} - {record.get('日期')} - {record.get('行程內容')}")
                    else:
                        logger.debug(f"日期不在範圍內: {schedule_date} 不在 {start_date} ~ {end_date}")
                except ValueError as date_e:
                    logger.warning(f"日期格式錯誤 {processed_count}: {record.get('日期')} - {date_e}")
                    continue
            
            logger.info(f"處理完成: 總記錄 {processed_count} 筆，符合條件 {len(schedules)} 筆")
            
            # 排序結果
            sorted_schedules = sorted(schedules, key=lambda x: (x['日期'], x.get('時間', '')))
            
            if sorted_schedules:
                logger.info(f"返回 {len(sorted_schedules)} 筆行程")
                for i, schedule in enumerate(sorted_schedules[:3]):  # 記錄前3筆
                    logger.info(f"結果 {i+1}: {schedule.get('ID')} - {schedule.get('日期')} {schedule.get('時間')} - {schedule.get('行程內容')}")
            else:
                logger.info("沒有找到符合條件的行程")
            
            return sorted_schedules
            
        except Exception as e:
            logger.error(f"查詢行程失敗: {e}")
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
            logger.info(f"查詢行程ID: {schedule_id}, 用戶: {user_id}")
            
            # 優先從 Google Sheets 讀取
            if USE_GOOGLE_SHEETS and self.sheet:
                try:
                    logger.info("從 Google Sheets 查詢行程ID...")
                    all_records = self.sheet.get_all_records()
                    logger.info(f"讀取到 {len(all_records)} 筆記錄")
                except Exception as e:
                    logger.error(f"從 Google Sheets 讀取失敗: {e}")
                    all_records = memory_storage
            else:
                logger.info("使用記憶體模式查詢行程ID")
                all_records = memory_storage
            
            for record in all_records:
                if (record.get('ID') == schedule_id and record.get('狀態') != '已刪除'):
                    logger.info(f"找到行程ID: {schedule_id}")
                    
                    # 如果指定了 user_id，檢查是否為該用戶的行程
                    if user_id and record.get('LINE用戶ID') != user_id:
                        logger.warning(f"行程ID {schedule_id} 不屬於用戶 {user_id}")
                        return None
                    
                    logger.info(f"返回行程: {record.get('日期')} - {record.get('行程內容')}")
                    return record
            
            logger.info(f"未找到行程ID: {schedule_id}")
            return None
            
        except Exception as e:
            logger.error(f"查詢行程 ID 失敗: {e}")
            return None
    
    def get_user_schedules_with_id(self, user_id, limit=10):
        try:
            logger.info(f"查詢用戶行程: {user_id}, 限制: {limit}")
            
            # 優先從 Google Sheets 讀取
            if USE_GOOGLE_SHEETS and self.sheet:
                try:
                    logger.info("從 Google Sheets 讀取用戶行程...")
                    all_records = self.sheet.get_all_records()
                    logger.info(f"讀取到 {len(all_records)} 筆總記錄")
                except Exception as e:
                    logger.error(f"從 Google Sheets 讀取失敗: {e}")
                    all_records = memory_storage
            else:
                logger.info("使用記憶體模式讀取用戶行程")
                all_records = memory_storage
            
            user_schedules = []
            processed_count = 0
            
            for record in all_records:
                processed_count += 1
                
                # 檢查是否為該用戶的有效行程
                if (record.get('LINE用戶ID') == user_id and 
                    record.get('狀態') != '已刪除' and
                    record.get('行程內容')):  # 確保有行程內容
                    user_schedules.append(record)
                    logger.debug(f"找到用戶行程: {record.get('ID')} - {record.get('行程內容')}")
            
            logger.info(f"用戶 {user_id} 共有 {len(user_schedules)} 筆有效行程")
            
            # 按建立時間排序，最新的在前
            try:
                user_schedules.sort(key=lambda x: x.get('建立時間', ''), reverse=True)
                logger.info("行程已按建立時間排序")
            except Exception as sort_e:
                logger.warning(f"排序失敗: {sort_e}")
            
            # 限制數量
            limited_schedules = user_schedules[:limit]
            
            if limited_schedules:
                logger.info(f"返回 {len(limited_schedules)} 筆用戶行程")
                for i, schedule in enumerate(limited_schedules[:3]):  # 記錄前3筆
                    logger.info(f"用戶行程 {i+1}: {schedule.get('ID')} - {schedule.get('日期')} - {schedule.get('行程內容')}")
            else:
                logger.info(f"用戶 {user_id} 沒有任何行程")
            
            return limited_schedules
            
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
        
        # 查詢 LINE ID 功能
        elif text in ["查詢ID", "查詢id", "ID", "id", "我的ID", "群組ID"]:
            # 判斷是群組還是個人
            if hasattr(event.source, 'group_id') and event.source.group_id:
                source_type = "群組"
                source_id = event.source.group_id
            elif hasattr(event.source, 'room_id') and event.source.room_id:
                source_type = "聊天室"
                source_id = event.source.room_id
            else:
                source_type = "個人"
                source_id = event.source.user_id
            
            reply_text = (f"🆔 LINE ID 資訊\n\n"
                         f"📱 類型: {source_type}\n"
                         f"🆔 {source_type}ID: {source_id}\n"
                         f"👤 您的用戶ID: {user_id}\n\n"
                         f"💡 提示：\n"
                         f"• 用戶ID用於個人行程管理\n"
                         f"• {source_type}ID用於識別對話來源")
        
        # 查詢行程ID功能（原本的查詢ID功能）
        elif text.startswith("查詢行程ID") or text.startswith("查詢行程id"):
            content = text.replace('查詢行程ID', '').replace('查詢行程id', '').strip()
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
                        reply_text = f"🔍 行程詳細資訊\n\n🆔 行程ID: {schedule_id}\n📅 日期: {friendly_date}\n⏰ 時間: {time}\n📝 內容: {content_text}\n🕐 建立時間: {created_time}"
                    else:
                        reply_text = f"🔍 行程詳細資訊\n\n🆔 行程ID: {schedule_id}\n📅 日期: {friendly_date} (全天)\n📝 內容: {content_text}\n🕐 建立時間: {created_time}"
                else:
                    reply_text = f"❌ 找不到行程 ID: {content}\n請確認行程ID是否正確，或該行程是否為您建立的"
            else:
                reply_text = "❌ 請輸入要查詢的行程 ID，格式：查詢行程ID S20240101120000001"
        
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
                
                reply_text += "💡 使用「查詢行程ID [ID號碼]」查看詳細資訊\n💡 使用「刪除行程ID [ID號碼]」刪除特定行程"
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
        
        # 刪除行程ID功能（原本的刪除ID功能）
        elif text.startswith("刪除行程ID") or text.startswith("刪除行程id"):
            content = text.replace('刪除行程ID', '').replace('刪除行程id', '').strip()
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
                    reply_text = f"✅ 已成功刪除行程\n📅 {friendly_date}\n📝 {content_text}\n🆔 行程ID: {content}"
                else:
                    reply_text = f"❌ 找不到行程 ID: {content}\n請確認行程ID是否正確，或該行程是否已被刪除"
            else:
                reply_text = "❌ 請輸入要刪除的行程 ID，格式：刪除行程ID S20240101120000001"
        
        # 功能說明
        elif text in ["功能", "menu", "選單", "菜單"]:
            reply_text = ("🎯 功能選單\n\n"
                         "📝 新增行程：直接輸入「今天10點開會」\n"
                         "🔍 查詢行程：「今日行程」「明日行程」等\n"
                         "🆔 查詢ID：「查詢ID」查看LINE群組/個人ID\n"
                         "📋 管理行程：「我的行程」查看所有行程ID\n"
                         "⏰ 倒數計時：「倒數 5 分鐘」\n"
                         "🔧 系統狀態：「狀態」\n\n"
                         "💡 快速範例：\n"
                         "• 明天10點開會\n"
                         "• 查詢ID（查看LINE ID）\n"
                         "• 查詢行程ID S123...（查看行程詳情）\n"
                         "• 刪除行程ID S123...（刪除行程）")
        
        elif text in ["幫助", "help", "使用說明", "?"]:
            reply_text = ("🤖 LINE Bot 行程管理系統\n\n"
                         "⚡ 快速使用：\n"
                         "• 明天10點開會 - 新增行程\n"
                         "• 今日行程 - 查詢今天行程\n"
                         "• 查詢ID - 查看LINE群組/個人ID\n"
                         "• 我的行程 - 查看所有行程及ID\n"
                         "• 倒數 5 分鐘 - 開始倒數計時\n\n"
                         "💡 輸入「功能」查看完整選單")
        
        elif text in ["測試", "test", "偵錯", "debug"]:
            try:
                debug_info = "🔍 系統偵錯資訊\n\n"
                
                # 檢查環境變數
                debug_info += f"🔑 Google Sheets 設定: {'✅ 已設定' if USE_GOOGLE_SHEETS else '❌ 未設定'}\n"
                
                if USE_GOOGLE_SHEETS and schedule_manager.sheet:
                    try:
                        # 讀取所有資料
                        all_records = schedule_manager.sheet.get_all_records()
                        debug_info += f"📊 Google Sheets 總記錄: {len(all_records)} 筆\n"
                        
                        # 統計用戶資料
                        user_records = [r for r in all_records if r.get('LINE用戶ID') == user_id]
                        debug_info += f"👤 您的記錄: {len(user_records)} 筆\n"
                        
                        # 有效記錄
                        valid_records = [r for r in user_records if r.get('狀態') != '已刪除' and r.get('行程內容')]
                        debug_info += f"✅ 有效行程: {len(valid_records)} 筆\n\n"
                        
                        # 顯示最近3筆記錄
                        if valid_records:
                            debug_info += "🗂️ 最近記錄:\n"
                            for i, record in enumerate(valid_records[:3], 1):
                                debug_info += f"{i}. ID: {record.get('ID', 'N/A')}\n"
                                debug_info += f"   日期: {record.get('日期', 'N/A')}\n"
                                debug_info += f"   內容: {record.get('行程內容', 'N/A')}\n"
                                debug_info += f"   狀態: {record.get('狀態', 'N/A')}\n\n"
                        else:
                            debug_info += "📝 沒有找到有效記錄\n\n"
                        
                        # 測試今日行程查詢
                        today = datetime.now(TZ).date()
                        today_schedules = schedule_manager.get_today_schedules(user_id)
                        debug_info += f"📅 今日行程查詢結果: {len(today_schedules)} 筆\n"
                        
                    except Exception as e:
                        debug_info += f"❌ Google Sheets 讀取錯誤: {str(e)[:100]}\n"
                else:
                    # 記憶體模式統計
                    memory_records = [r for r in memory_storage if r.get('LINE用戶ID') == user_id]
                    debug_info += f"📱 記憶體模式記錄: {len(memory_records)} 筆\n"
                
                debug_info += f"\n🕐 檢查時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}"
                
                reply_text = debug_info
                
            except Exception as e:
                reply_text = f"🔍 偵錯功能錯誤: {str(e)}"
            try:
                # 檢測 Google Sheets 連接狀態
                if USE_GOOGLE_SHEETS and schedule_manager.sheet:
                    try:
                        # 測試讀取
                        test_records = schedule_manager.sheet.get_all_records()
                        sheets_status = "✅ Google Sheets 連接正常"
                        total_records = len([r for r in test_records if r.get('行程內容')])
                        user_records = len([r for r in test_records if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
                        
                        # 測試寫入權限
                        try:
                            test_id = f"TEST{datetime.now(TZ).strftime('%H%M%S')}"
                            test_row = [test_id, '2099-12-31', '23:59', '連接測試', '', datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'), 'TEST_USER', '測試']
                            schedule_manager.sheet.append_row(test_row)
                            
                            # 立即刪除測試資料
                            import time
                            time.sleep(1)
                            all_values = schedule_manager.sheet.get_all_values()
                            for i, row in enumerate(all_values):
                                if len(row) > 0 and row[0] == test_id:
                                    schedule_manager.sheet.delete_rows(i + 1)
                                    break
                            
                            sheets_status += " (讀寫正常)"
                            
                        except Exception as write_e:
                            sheets_status = f"⚠️ Google Sheets 只讀模式 (寫入失敗: {str(write_e)[:50]}...)"
                            
                    except Exception as read_e:
                        sheets_status = f"❌ Google Sheets 連接異常 (讀取失敗: {str(read_e)[:50]}...)"
                        total_records = 0
                        user_records = 0
                else:
                    sheets_status = "📱 記憶體模式運行"
                    total_records = len([r for r in memory_storage if r.get('行程內容')])
                    user_records = len([r for r in memory_storage if r.get('LINE用戶ID') == user_id and r.get('狀態') != '已刪除'])
                    
            except Exception as e:
                logger.error(f"狀態檢查失敗: {e}")
                sheets_status = f"❌ 狀態檢查失敗: {str(e)[:50]}..."
                total_records = 0
                user_records = 0
            
            # 環境變數檢查
            env_status = "✅ 完整" if USE_GOOGLE_SHEETS else "⚠️ 缺少 Google Sheets 設定"
            
            reply_text = (f"🔧 系統狀態報告\n\n"
                         f"📊 資料儲存: {sheets_status}\n"
                         f"🔑 環境變數: {env_status}\n"
                         f"📈 總行程數: {total_records}\n"
                         f"👤 您的行程數: {user_records}\n"
                         f"🕐 系統時間: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
                         f"🌏 時區: Asia/Taipei\n"
                         f"🤖 LINE SDK: v{LINEBOT_SDK_VERSION}")
        
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
