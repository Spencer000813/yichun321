# app.py
import os
import re
import json
from datetime import datetime, timedelta
from flask import Flask, request, abort
import pytz
import atexit

import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler

# 使用新版本的 LINE Bot SDK
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

# === 初始化 ===
TZ = pytz.timezone("Asia/Taipei")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

# 新版 LINE Bot SDK 設定
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# === Google Sheet 認證 ===
# 添加錯誤處理和除錯資訊
try:
    google_creds_raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not google_creds_raw:
        raise ValueError("GOOGLE_CREDENTIALS_JSON 環境變數未設定")
    
    print(f"憑證長度: {len(google_creds_raw)}")
    print(f"前 100 個字元: {google_creds_raw[:100]}")
    
    SERVICE_ACCOUNT_INFO = json.loads(google_creds_raw)
    print("JSON 解析成功")
    
except json.JSONDecodeError as e:
    print(f"JSON 解析錯誤: {e}")
    print(f"錯誤位置: 第 {e.lineno} 行，第 {e.colno} 列")
    if google_creds_raw and len(google_creds_raw) > e.pos:
        print(f"錯誤字元: '{google_creds_raw[e.pos]}'")
        print(f"錯誤附近內容: {google_creds_raw[max(0, e.pos-20):e.pos+20]}")
    raise
except Exception as e:
    print(f"其他錯誤: {e}")
    raise

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)

# 添加 Google Sheet 初始化錯誤處理
try:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID 環境變數未設定")
    
    sheet = gc.open_by_key(spreadsheet_id).sheet1
    print("Google Sheet 連接成功")
    
    # 檢查並設置標題行（如果需要）
    try:
        headers = sheet.row_values(1)
        if not headers or len(headers) < 4:
            sheet.clear()
            sheet.append_row(['使用者 ID', '日期', '時間', '行程內容'])
            print("已設置 Google Sheet 標題行")
    except Exception as e:
        print(f"設置標題行時發生錯誤: {e}")
        
except Exception as e:
    print(f"Google Sheet 初始化錯誤: {e}")
    raise

# === 寫入行程到 Google Sheet ===
def add_schedule(date_str, time_str, content, user_id):
    try:
        sheet.append_row([user_id, date_str, time_str, content])
        print(f"成功新增行程: {user_id}, {date_str}, {time_str}, {content}")
        return True
    except Exception as e:
        print(f"新增行程失敗: {e}")
        return False

# === 查詢行程 ===
def query_schedule_by_range(user_id, start_date, end_date):
    try:
        records = sheet.get_all_records()
        result = []
        for r in records:
            # 處理可能的空白或不同格式的使用者 ID
            record_user_id = str(r.get('使用者 ID', '')).strip()
            if record_user_id == user_id:
                try:
                    # 處理不同的日期格式
                    date_str = str(r.get('日期', '')).strip()
                    if '/' in date_str:
                        date_obj = datetime.strptime(date_str, '%Y/%m/%d')
                    elif '-' in date_str:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    else:
                        continue
                    
                    if start_date <= date_obj.date() <= end_date:
                        result.append(r)
                except Exception as parse_error:
                    print(f"日期解析錯誤: {date_str}, 錯誤: {parse_error}")
                    continue
        return result
    except Exception as e:
        print(f"查詢行程錯誤: {e}")
        return []

# === 處理訊息 ===
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # 查詢 ID（不分大小寫）
    if text.lower() in ["查id", "查 id"]:
        reply = f"你的 ID 是：{user_id}"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)]
                )
            )
        return

    today = datetime.now(TZ).date()
    schedules = []
    
    if text == "今日行程":
        schedules = query_schedule_by_range(user_id, today, today)
    elif text == "明日行程":
        tomorrow = today + timedelta(days=1)
        schedules = query_schedule_by_range(user_id, tomorrow, tomorrow)
    elif text == "下週行程":
        # 計算下週一到下週日
        days_until_next_monday = (7 - today.weekday()) % 7
        if days_until_next_monday == 0:  # 如果今天是週一，取下週一
            days_until_next_monday = 7
        next_monday = today + timedelta(days=days_until_next_monday)
        next_sunday = next_monday + timedelta(days=6)
        schedules = query_schedule_by_range(user_id, next_monday, next_sunday)
    elif text == "下個月行程":
        # 計算下個月的第一天和最後一天
        if today.month == 12:
            next_month = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month = today.replace(month=today.month + 1, day=1)
        
        # 計算下個月的最後一天
        if next_month.month == 12:
            end_next_month = next_month.replace(year=next_month.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end_next_month = next_month.replace(month=next_month.month + 1, day=1) - timedelta(days=1)
        
        schedules = query_schedule_by_range(user_id, next_month, end_next_month)
    elif text == "明年行程":
        next_year = today.replace(month=1, day=1, year=today.year + 1)
        end_next_year = next_year.replace(month=12, day=31)
        schedules = query_schedule_by_range(user_id, next_year, end_next_year)
    else:
        # 嘗試解析新增行程格式: 7/14 10:00 開會
        match = re.match(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)", text)
        if match:
            month, day, time_str, content = match.groups()
            year = today.year
            try:
                # 處理跨年情況
                date_obj = datetime.strptime(f"{year}/{month}/{day}", "%Y/%m/%d")
                if date_obj.date() < today:
                    date_obj = date_obj.replace(year=year + 1)
                
                success = add_schedule(date_obj.strftime('%Y/%m/%d'), time_str, content, user_id)
                if success:
                    reply = f"✅ 已新增行程：{date_obj.strftime('%m/%d')} {time_str} {content}"
                else:
                    reply = "❌ 新增行程失敗，請稍後再試"
            except ValueError:
                reply = "❌ 日期格式錯誤，請用 MM/DD HH:MM 內容"
            except Exception as e:
                print(f"新增行程時發生錯誤: {e}")
                reply = "❌ 新增行程時發生錯誤"
        else:
            reply = "請輸入正確格式：\n• 新增行程：MM/DD HH:MM 內容\n• 查詢：今日行程、明日行程、下週行程等\n• 查詢ID：查ID"
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)]
                )
            )
        return

    # 處理查詢結果
    if schedules:
        msg = "📌 查詢結果：\n\n"
        for s in schedules:
            msg += f"📅 {s.get('日期', 'N/A')} {s.get('時間', 'N/A')}\n📝 {s.get('行程內容', 'N/A')}\n\n"
    else:
        msg = "沒有找到相關行程。"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg.strip())]
            )
        )

# === 每週五推播兩週後的行程 ===
def friday_reminder():
    try:
        records = sheet.get_all_records()
        future_date = datetime.now(TZ).date() + timedelta(days=14)
        user_schedules = {}
        
        for r in records:
            try:
                date_str = str(r.get('日期', '')).strip()
                if '/' in date_str:
                    date_obj = datetime.strptime(date_str, "%Y/%m/%d").date()
                elif '-' in date_str:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                else:
                    continue
                    
                if date_obj == future_date:
                    uid = str(r.get('使用者 ID', '')).strip()
                    if uid:
                        user_schedules.setdefault(uid, []).append(r)
            except Exception as e:
                print(f"處理記錄時發生錯誤: {e}")
                continue

        for uid, items in user_schedules.items():
            msg = f"🔔 兩週後（{future_date.strftime('%Y/%m/%d')}）的行程提醒：\n\n"
            for s in items:
                msg += f"📅 {s.get('日期', 'N/A')} {s.get('時間', 'N/A')}\n📝 {s.get('行程內容', 'N/A')}\n\n"
            
            try:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.push_message_with_http_info(
                        PushMessageRequest(
                            to=uid,
                            messages=[TextMessage(text=msg.strip())]
                        )
                    )
                print(f"推播成功: {uid}")
            except Exception as e:
                print(f"推播失敗 {uid}: {e}")
                
    except Exception as e:
        print(f"提醒功能錯誤: {e}")

# 排程器設定
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(friday_reminder, 'cron', day_of_week='fri', hour=10, minute=0)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)
    
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature error")
        abort(400)
    except Exception as e:
        print(f"Webhook 處理錯誤: {e}")
        abort(500)
    
    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "LINE Bot is running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"應用程式啟動在端口 {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
