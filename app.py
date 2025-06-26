import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort
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

# LINE Bot SDK 版本檢測和導入
LINEBOT_SDK_VERSION = 2
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
if not GOOGLE_CREDENTIALS or not SPREADSHEET_ID:
    logger.error("缺少 Google Sheets 環境變數")
    raise ValueError("請設定 GOOGLE_CREDENTIALS 和 SPREADSHEET_ID 環境變數")

# 時區設定
TZ = pytz.timezone('Asia/Taipei')

class ScheduleManager:
    def __init__(self):
        self.gc = None
        self.sheet = None
        self.setup_google_sheets()

    def setup_google_sheets(self):
        try:
            logger.info("開始設定 Google Sheets 連接...")
            credentials_dict = json.loads(GOOGLE_CREDENTIALS)
            creds = Credentials.from_service_account_info(
                credentials_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            self.gc = gspread.authorize(creds)
            self.sheet = self.gc.open_by_key(SPREADSHEET_ID).sheet1
            logger.info(f"成功開啟試算表: {SPREADSHEET_ID}")

            # 確保表頭存在
            headers = ['ID', '日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID', '狀態']
            existing_headers = self.sheet.row_values(1)
            if not existing_headers or len(existing_headers) < len(headers):
                self.sheet.update('A1:H1', [headers])
                logger.info("表頭設定完成")
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
            
            schedule_id = f"S{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
            created_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            row = [schedule_id, date_str, time_str or '', content, reminder or '', created_time, user_id, '有效']
            
            self.sheet.append_row(row)
            logger.info(f"成功寫入 Google Sheets: {schedule_id}")
            return schedule_id
            
        except ValueError as e:
            logger.error(f"日期時間格式錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"新增行程失敗: {e}")
            return False

    def get_schedules_by_date_range(self, start_date, end_date, user_id=None):
        try:
            all_records = self.sheet.get_all_records()
            schedules = []
            for record in all_records:
                if (record.get('狀態') == '已刪除' or 
                   (user_id and record.get('LINE用戶ID') != user_id)):
                    continue
                try:
                    schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                    if start_date <= schedule_date <= end_date:
                        schedules.append(record)
                except ValueError:
                    continue
            return sorted(schedules, key=lambda x: (x['日期'], x.get('時間', '')))
        except Exception as e:
            logger.error(f"查詢行程失敗: {e}")
            return []

    # 其他查詢方法（今日/明日/本週等）保持不變，僅調用get_schedules_by_date_range
    def get_today_schedules(self, user_id):
        today = datetime.now(TZ).date()
        return self.get_schedules_by_date_range(today, today, user_id)
    
    # ... 其他查詢方法（明日/本週等）依此類推

    def delete_schedule_by_id(self, schedule_id, user_id):
        try:
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
            return None
        except Exception as e:
            logger.error(f"刪除行程 ID 失敗: {e}")
            return None

schedule_manager = ScheduleManager()

# 以下函式保持不變（格式處理、輸入解析等）
def format_schedules(schedules, title):
    # ... 與原始碼相同

def is_schedule_input(text):
    # ... 與原始碼相同

def parse_schedule_input(text):
    # ... 與原始碼相同

def parse_natural_input(text):
    # ... 與原始碼相同

# Flask路由和事件處理保持不變
@app.route("/", methods=["GET"])
def health_check():
    return "LINE Bot 行程管理系統運行中 (Google Sheets 模式)", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    # ... 與原始碼相同

@handler.add(MessageEvent, message=(TextMessageContent if LINEBOT_SDK_VERSION == 3 else TextMessage))
def handle_message(event):
    # ... 與原始碼相同（包含指令處理邏輯）

# 排程器設定保持不變
scheduler = BackgroundScheduler(timezone=TZ)
# ... 其他初始化程式碼
