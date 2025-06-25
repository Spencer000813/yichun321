import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

app = Flask(__name__)

# LINE Bot 驗證資料
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 設定
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")  # JSON 格式的服務帳戶金鑰
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  # Google Sheets 的 ID

# 時區設定
TZ = pytz.timezone('Asia/Taipei')

# 從環境變數載入憑證並初始化 Google Sheets 連接
credentials = json.loads(os.environ['GOOGLE_CREDENTIALS'])
gc = gspread.service_account_from_dict(credentials)
sheet = gc.open_by_key(os.environ.get('SPREADSHEET_ID', '1mQODCqq5Kont66zp1M8_xXnzPSeP4osZcRlk9WAWRn8')).sheet1

class ScheduleManager:
    def __init__(self):
        self.setup_google_sheets()
    
    def setup_google_sheets(self):
        """設定 Google Sheets 連接"""
        try:
            # 使用全域的 gc 和 sheet 變數
            global gc, sheet
            self.gc = gc
            self.sheet = sheet
            
            # 確保表頭存在
            headers = ['日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID']
            if not self.sheet.row_values(1):
                self.sheet.insert_row(headers, 1)
        except Exception as e:
            print(f"Google Sheets 連接失敗: {e}")
            # 如果全域連接失敗，嘗試原有的連接方式作為備援
            try:
                credentials_dict = json.loads(GOOGLE_CREDENTIALS)
                creds = Credentials.from_service_account_info(
                    credentials_dict,
                    scopes=['https://www.googleapis.com/auth/spreadsheets']
                )
                self.gc = gspread.authorize(creds)
                self.sheet = self.gc.open_by_key(SPREADSHEET_ID).sheet1
                
                # 確保表頭存在
                headers = ['日期', '時間', '行程內容', '提醒設定', '建立時間', 'LINE用戶ID']
                if not self.sheet.row_values(1):
                    self.sheet.insert_row(headers, 1)
            except Exception as backup_error:
                print(f"備援 Google Sheets 連接也失敗: {backup_error}")
    
    def add_schedule(self, date_str, time_str, content, user_id, reminder=None):
        """新增行程到 Google Sheets"""
        try:
            created_time = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
            row = [date_str, time_str, content, reminder or '', created_time, user_id]
            self.sheet.append_row(row)
            return True
        except Exception as e:
            print(f"新增行程失敗: {e}")
            return False
    
    def get_schedules_by_date_range(self, start_date, end_date, user_id=None):
        """取得指定日期範圍的行程"""
        try:
            all_records = self.sheet.get_all_records()
            schedules = []
            
            for record in all_records:
                if user_id and record['LINE用戶ID'] != user_id:
                    continue
                
                try:
                    schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                    if start_date <= schedule_date <= end_date:
                        schedules.append(record)
                except:
                    continue
            
            return sorted(schedules, key=lambda x: (x['日期'], x['時間']))
        except Exception as e:
            print(f"取得行程失敗: {e}")
            return []
    
    def get_today_schedules(self, user_id):
        """取得今日行程"""
        today = datetime.now(TZ).date()
        return self.get_schedules_by_date_range(today, today, user_id)
    
    def get_tomorrow_schedules(self, user_id):
        """取得明日行程"""
        tomorrow = datetime.now(TZ).date() + timedelta(days=1)
        return self.get_schedules_by_date_range(tomorrow, tomorrow, user_id)
    
    def get_next_week_schedules(self, user_id):
        """取得下周行程"""
        today = datetime.now(TZ).date()
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        return self.get_schedules_by_date_range(next_monday, next_sunday, user_id)
    
    def get_next_month_schedules(self, user_id):
        """取得下個月行程"""
        today = datetime.now(TZ).date()
        if today.month == 12:
            next_month_start = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month_start = today.replace(month=today.month + 1, day=1)
        
        # 下個月最後一天
        if next_month_start.month == 12:
            next_month_end = next_month_start.replace(year=next_month_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            next_month_end = next_month_start.replace(month=next_month_start.month + 1, day=1) - timedelta(days=1)
        
        return self.get_schedules_by_date_range(next_month_start, next_month_end, user_id)
    
    def get_next_year_schedules(self, user_id):
        """取得明年行程"""
        today = datetime.now(TZ).date()
        next_year_start = today.replace(year=today.year + 1, month=1, day=1)
        next_year_end = today.replace(year=today.year + 1, month=12, day=31)
        return self.get_schedules_by_date_range(next_year_start, next_year_end, user_id)
    
    def get_two_weeks_later_schedules(self):
        """取得兩週後的行程（用於週五推播）"""
        today = datetime.now(TZ).date()
        two_weeks_later = today + timedelta(weeks=2)
        start_of_week = two_weeks_later - timedelta(days=two_weeks_later.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        all_records = self.sheet.get_all_records()
        schedules_by_user = {}
        
        for record in all_records:
            try:
                schedule_date = datetime.strptime(record['日期'], '%Y-%m-%d').date()
                if start_of_week <= schedule_date <= end_of_week:
                    user_id = record['LINE用戶ID']
                    if user_id not in schedules_by_user:
                        schedules_by_user[user_id] = []
                    schedules_by_user[user_id].append(record)
            except:
                continue
        
        return schedules_by_user

# 初始化行程管理器
schedule_manager = ScheduleManager()

def format_schedules(schedules, title):
    """格式化行程輸出"""
    if not schedules:
        return f"{title}\n📅 目前沒有安排任何行程"
    
    message = f"{title}\n"
    for schedule in schedules:
        date = schedule['日期']
        time = schedule['時間'] if schedule['時間'] else '全天'
        content = schedule['行程內容']
        message += f"📅 {date} {time}\n📝 {content}\n\n"
    
    return message.strip()

def is_schedule_input(text):
    """判斷是否為行程輸入格式"""
    import re
    
    # 檢查是否符合自然語言行程輸入格式
    patterns = [
        r'\d{1,2}/\d{1,2}',  # 7/14 格式
        r'\d{1,2}月\d{1,2}[號日]',  # 6月30號 格式
        r'\d{4}-\d{1,2}-\d{1,2}',  # 2024-07-14 格式
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
    import re
    from datetime import datetime
    
    current_year = datetime.now().year
    
    # 正規表達式模式
    patterns = [
        # 7/14 10:00 開會 或 07/14 10:00 開會
        (r'(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)', 'date_time'),
        # 7/14 開會 或 07/14 開會 (全天)
        (r'(\d{1,2})/(\d{1,2})\s+(.+)', 'date_only'),
        # 6月30號 下午2點 盤點
        (r'(\d{1,2})月(\d{1,2})[號日]\s*下午(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        # 6月30號 上午10點 開會
        (r'(\d{1,2})月(\d{1,2})[號日]\s*上午(\d{1,2})[點時]\s*(.+)', 'chinese_am'),
        # 6月30號 晚上8點 聚餐
        (r'(\d{1,2})月(\d{1,2})[號日]\s*晚上(\d{1,2})[點時]\s*(.+)', 'chinese_pm'),
        # 6月30號 10點 開會 (預設上午)
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(\d{1,2})[點時]\s*(.+)', 'chinese_default'),
        # 6月30號 盤點 (全天)
        (r'(\d{1,2})月(\d{1,2})[號日]\s*(.+)', 'chinese_date_only'),
        # 2024-07-14 10:00 開會 (原格式支援)
        (r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)', 'full_date_time'),
        # 2024-07-14 開會 (原格式全天)
        (r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(.+)', 'full_date_only'),
    ]
    
    for pattern, pattern_type in patterns:
        match = re.match(pattern, text.strip())
        if match:
            try:
                if pattern_type == 'date_time':
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
                    # 預設為上午，除非是下午時間
                    if hour >= 13:
                        pass  # 24小時制
                    elif hour == 12:
                        pass  # 中午12點
                    else:
                        pass  # 保持原時間
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{hour:02d}:00"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'chinese_date_only':
                    month, day, content = match.groups()
                    date_str = f"{current_year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                
                elif pattern_type == 'full_date_time':
                    year, month, day, hour, minute, content = match.groups()
                    date_str = f"{year}-{int(month):02d}-{int(day):02d}"
                    time_str = f"{int(hour):02d}:{minute}"
                    return date_str, time_str, content.strip()
                
                elif pattern_type == 'full_date_only':
                    year, month, day, content = match.groups()
                    date_str = f"{year}-{int(month):02d}-{int(day):02d}"
                    return date_str, '', content.strip()
                
            except (ValueError, IndexError):
                continue
    
    return None, None, None

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    
    # 倒數計時功能（原有功能）
    if text.startswith("倒數") and "分鐘" in text:
        try:
            minute = int(text.replace("倒數", "").replace("分鐘", "").strip())
            if 0 < minute <= 60:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"倒數 {minute} 分鐘開始！我會在時間到時提醒你。")
                )
                # 計算剩餘時間後自動推送訊息
                from threading import Timer
                target_id = event.source.group_id if event.source.type == "group" else event.source.user_id
                def send_reminder():
                    line_bot_api.push_message(
                        target_id,
                        TextSendMessage(text=f"⏰ {minute} 分鐘倒數結束，時間到囉！")
                    )
                Timer(minute * 60, send_reminder).start()
                return
        except:
            pass
    
    # 查詢行程功能
    if text == "今日行程":
        schedules = schedule_manager.get_today_schedules(user_id)
        reply_text = format_schedules(schedules, "📅 今日行程")
    
    elif text == "明日行程":
        schedules = schedule_manager.get_tomorrow_schedules(user_id)
        reply_text = format_schedules(schedules, "📅 明日行程")
    
    elif text == "下周行程":
        schedules = schedule_manager.get_next_week_schedules(user_id)
        reply_text = format_schedules(schedules, "📅 下周行程")
    
    elif text == "下個月行程":
        schedules = schedule_manager.get_next_month_schedules(user_id)
        reply_text = format_schedules(schedules, "📅 下個月行程")
    
    elif text == "明年行程":
        schedules = schedule_manager.get_next_year_schedules(user_id)
        reply_text = format_schedules(schedules, "📅 明年行程")
    
    # 新增行程功能（支援多種格式）
    elif text.startswith("新增行程") or is_schedule_input(text):
        # 如果不是以「新增行程」開頭，自動加上前綴
        if not text.startswith("新增行程"):
            text = "新增行程 " + text
            
        date_str, time_str, content = parse_schedule_input(text)
        
        if date_str and content:
            success = schedule_manager.add_schedule(date_str, time_str, content, user_id)
            if success:
                time_display = f" {time_str}" if time_str else " (全天)"
                reply_text = f"✅ 行程已新增！\n📅 {date_str}{time_display}\n📝 {content}"
            else:
                reply_text = "❌ 新增行程失敗，請稍後再試"
        else:
            reply_text = ("❌ 格式錯誤！支援以下格式：\n\n"
                         "📝 直接輸入（不用加『新增行程』）：\n"
                         "• 7/14 10:00 開會\n"
                         "• 7/14 聚餐\n"
                         "• 6月30號 下午2點 盤點\n"
                         "• 12月25號 聖誕節\n\n"
                         "📝 完整格式：\n"
                         "• 新增行程 2024-12-25 09:30 會議\n"
                         "• 新增行程 2024-12-25 聖誕節")
    
    # 幫助訊息
    elif text in ["幫助", "help", "使用說明"]:
        reply_text = ("📋 行程管理功能說明：\n\n"
                     "📝 新增行程（支援多種格式）：\n"
                     "• 7/14 10:00 開會\n"
                     "• 7/14 聚餐\n"
                     "• 6月30號 下午2點 盤點\n"
                     "• 12月25號 聖誕節\n"
                     "• 新增行程 2024-12-25 09:30 會議\n\n"
                     "🔍 查詢行程：\n"
                     "• 今日行程\n"
                     "• 明日行程\n"
                     "• 下周行程\n"
                     "• 下個月行程\n"
                     "• 明年行程\n\n"
                     "⏰ 倒數計時：\n"
                     "• 倒數 5 分鐘\n\n"
                     "📢 系統會在每週五早上10點推播兩週後的行程提醒")
    
    # 倒數計時格式錯誤提醒
    elif text.startswith("倒數"):
        reply_text = "請輸入格式：倒數 X 分鐘，例如：倒數 5 分鐘"
    
    else:
        reply_text = "請輸入「幫助」查看使用說明，或直接輸入行程資訊"
    
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def friday_reminder():
    """週五早上10點推播兩週後行程"""
    schedules_by_user = schedule_manager.get_two_weeks_later_schedules()
    
    for user_id, schedules in schedules_by_user.items():
        if schedules:
            message = "🔔 兩週後行程提醒\n\n"
            for schedule in sorted(schedules, key=lambda x: (x['日期'], x['時間'])):
                date = schedule['日期']
                time = schedule['時間'] if schedule['時間'] else '全天'
                content = schedule['行程內容']
                message += f"📅 {date} {time}\n📝 {content}\n\n"
            
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=message.strip()))
            except Exception as e:
                print(f"推播失敗 {user_id}: {e}")

# 設定排程器
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(
    friday_reminder,
    'cron',
    day_of_week='fri',
    hour=10,
    minute=0
)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
