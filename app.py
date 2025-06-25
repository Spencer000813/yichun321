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

from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

# === 初始化 ===
TZ = pytz.timezone("Asia/Taipei")
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("h/oY5VpyrVOEEQDKVNO3N2bS4dJjq6HF+DXmu1boCTIn9aOOqWSu+Lkh1I/gZOaLgF6glocNn3H6FrVLJjqlc0AW+WEfpSct5eQDPj8AmS5o9bMLpqRFTXs7jcFpiEXd2ECVd0yznD7cM4TEk7dkTwdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.getenv("ce86d81dd615983bca5766bca2833894")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# === Google Sheet 認證 ===
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(os.getenv("1mQODCqq5Kont66zp1M8_xXnzPSeP4osZcRlk9WAWRn8")).sheet1

# === 寫入行程到 Google Sheet ===
def add_schedule(date_str, time_str, content, user_id):
    sheet.append_row([user_id, date_str, time_str, content])

# === 查詢行程 ===
def query_schedule_by_range(user_id, start_date, end_date):
    records = sheet.get_all_records()
    result = []
    for r in records:
        if r['使用者 ID'] == user_id:
            try:
                date_obj = datetime.strptime(r['日期'], '%Y/%m/%d')
                if start_date <= date_obj.date() <= end_date:
                    result.append(r)
            except:
                continue
    return result

# === 處理訊息 ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # 查詢 ID
    if text.lower() in ["查id", "查ID"]:
        reply = f"你的 ID 是：{user_id}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    today = datetime.now(TZ).date()
    if text == "今日行程":
        schedules = query_schedule_by_range(user_id, today, today)
    elif text == "明日行程":
        schedules = query_schedule_by_range(user_id, today + timedelta(days=1), today + timedelta(days=1))
    elif text == "下週行程":
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_sunday = next_monday + timedelta(days=6)
        schedules = query_schedule_by_range(user_id, next_monday, next_sunday)
    elif text == "下個月行程":
        next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        end_next_month = (next_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
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
                date_obj = datetime.strptime(f"{year}/{month}/{day}", "%Y/%m/%d")
                add_schedule(date_obj.strftime('%Y/%m/%d'), time_str, content, user_id)
                reply = f"✅ 已新增行程：{date_obj.strftime('%m/%d')} {time_str} {content}"
            except:
                reply = "❌ 日期格式錯誤，請用 MM/DD HH:MM 內容"
        else:
            reply = "請輸入正確格式新增行程，例如：7/14 10:00 開會"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if schedules:
        msg = "📌 查詢結果：\n\n"
        for s in schedules:
            msg += f"📅 {s['日期']} {s['時間']}\n📝 {s['行程內容']}\n\n"
    else:
        msg = "沒有找到相關行程。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg.strip()))

# === 每週五推播兩週後的行程 ===
def friday_reminder():
    try:
        records = sheet.get_all_records()
        future_date = datetime.now(TZ).date() + timedelta(days=14)
        user_schedules = {}
        for r in records:
            try:
                date_obj = datetime.strptime(r['日期'], "%Y/%m/%d").date()
                if date_obj == future_date:
                    uid = r['使用者 ID']
                    user_schedules.setdefault(uid, []).append(r)
            except:
                continue

        for uid, items in user_schedules.items():
            msg = "🔔 兩週後的行程提醒：\n\n"
            for s in items:
                msg += f"📅 {s['日期']} {s['時間']}\n📝 {s['行程內容']}\n\n"
            try:
                line_bot_api.push_message(uid, TextSendMessage(text=msg.strip()))
                print(f"推播成功: {uid}")
            except Exception as e:
                print(f"推播失敗 {uid}: {e}")
    except Exception as e:
        print(f"提醒錯誤: {e}")

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
        abort(400)
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
