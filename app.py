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

# LINE 驗證
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

# 時區
TZ = pytz.timezone("Asia/Taipei")

# 記憶倒數任務
pending_countdowns = {}

@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    source_type = event.source.type

    # 查 ID
    if text.lower() == "查id":
        if source_type == "user":
            reply = f"你的使用者 ID 是：\n{user_id}"
        elif source_type == "group":
            reply = f"群組 ID 是：\n{event.source.group_id}"
        elif source_type == "room":
            reply = f"聊天室 ID 是：\n{event.source.room_id}"
        else:
            reply = "無法辨識來源類型"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))
        return

    # 倒數計時
    if text.startswith("倒數") and "分鐘" in text:
        try:
            mins = int(text.replace("倒數", "").replace("分鐘", "").strip())
            if mins <= 0:
                raise ValueError
            reply = f"⏳ 已開始倒數 {mins} 分鐘。"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))

            # 安排提醒
            scheduler.add_job(
                func=send_countdown_done,
                trigger='date',
                run_date=datetime.now(TZ) + timedelta(minutes=mins),
                args=[event.source],
                id=f"countdown_{user_id}_{datetime.now().timestamp()}"
            )
            return
        except ValueError:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="請輸入正確的分鐘數。"))
            return

    # 查行程
    today = datetime.now(TZ).date()
    text_map = {
        "今天有哪些行程": today,
        "明天有哪些行程": today + timedelta(days=1),
        "本週有哪些行程": "week",
        "下週有哪些行程": "nextweek"
    }
    if text in text_map:
        keyword = text_map[text]
        data = sheet.get_all_records()
        result = []
        for row in data:
            row_date = datetime.strptime(row['日期'], "%Y-%m-%d").date()
            if (keyword == "week" and today <= row_date <= today + timedelta(days=6)) or \
               (keyword == "nextweek" and today + timedelta(days=7) <= row_date <= today + timedelta(days=13)) or \
               (keyword == row_date):
                result.append(row)
        if not result:
            reply = "\n目前沒有安排任何行程"
        else:
            reply = f"\n\n".join([f"📅 {r['日期']}\n📝 {r['行程內容']}" for r in result])
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))
        return

    # 預設訊息
    line_bot_api.reply_message(reply_token, TextSendMessage(text="請輸入『查ID』或『倒數3分鐘』或『今天有哪些行程』等指令"))

# 倒數結束提醒
def send_countdown_done(source):
    id_ = source.user_id if source.type == "user" else source.group_id
    line_bot_api.push_message(id_, TextSendMessage(text="⏰ 3分鐘已到！"))

# 每週五 10:00 自動提醒
def friday_reminder():
    today = datetime.now(TZ).date()
    data = sheet.get_all_records()
    result = [r for r in data if r['日期'] == today.strftime("%Y-%m-%d")]
    if result:
        msg = f"今天的提醒：\n\n" + "\n\n".join([f"📅 {r['日期']}\n📝 {r['行程內容']}" for r in result])
        # 群組 ID or user ID 手動設定
        target_id = os.getenv("REMIND_TARGET_ID")
        if target_id:
            line_bot_api.push_message(target_id, TextSendMessage(text=msg))

# 啟動 APScheduler
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(friday_reminder, 'cron', day_of_week='fri', hour=10, minute=0)
scheduler.start()

# Render 必須偵測 PORT
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
