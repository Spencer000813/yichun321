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

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

TZ = pytz.timezone('Asia/Taipei')

credentials = json.loads(os.environ['GOOGLE_CREDENTIALS'])
gc = gspread.service_account_from_dict(credentials)
sheet = gc.open_by_key(os.environ.get('SPREADSHEET_ID', '1mQODCqq5Kont66zp1M8_xXnzPSeP4osZcRlk9WAWRn8')).sheet1

class ScheduleManager:
    ...  # (保留原有的 ScheduleManager 類別完整內容，略去顯示)

schedule_manager = ScheduleManager()

def format_schedules(schedules, title):
    if not schedules:
        return f"{title}\n\ud83d\uddd3\ufe0f 目前沒有安排任何行程"
    message = f"{title}\n"
    for schedule in schedules:
        date = schedule['日期']
        time = schedule['時間'] if schedule['時間'] else '全天'
        content = schedule['行程內容']
        message += f"\ud83d\uddd3\ufe0f {date} {time}\n\ud83d\udcdd {content}\n\n"
    return message.strip()

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

    if text.lower() == "查id":
        source_type = event.source.type
        if source_type == "user":
            reply_text = f"\ud83d\udc64 你的使用者 ID 是：\n{event.source.user_id}"
        elif source_type == "group":
            reply_text = f"\ud83d\udc65 群組 ID 是：\n{event.source.group_id}"
        elif source_type == "room":
            reply_text = f"\ud83d\udde3\ufe0f 聊天室 ID 是：\n{event.source.room_id}"
        else:
            reply_text = "\u2753 無法辨識來源類型"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        return

    if text.startswith("倒數") and "分鐘" in text:
        ...  # (保留倒數功能區塊程式碼)

    # 查行程、增加行程與幫助訊息處理區塊...
    ...

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def friday_reminder():
    ...  # (保留提醒功能完整程式碼)

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
