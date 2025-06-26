import os
import json
from datetime import datetime, timedelta
from flask import Flask, request, abort

import gspread
from google.oauth2.service_account import Credentials

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# 使用新版本的 LINE Bot SDK
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, TextMessage, PushMessageRequest
from linebot.v3.webhook import WebhookHandler, WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# 初始化 Flask 與 APScheduler
app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# 環境變數檢查與初始化
REQUIRED_ENV_VARS = [
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET",
    "GOOGLE_CREDENTIALS_JSON",
    "GOOGLE_SPREADSHEET_ID"
]

missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    print(f"❌ 缺少必要的環境變數: {', '.join(missing_vars)}")
    exit(1)

# LINE 機器人初始化
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 初始化
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    if user_text.lower() == "debug":
        try:
            data = sheet.get_all_records()
            if not data:
                reply = "❌ 沒有讀到任何資料，請檢查欄位名稱和內容。"
            else:
                reply = f"✅ 成功讀取 {len(data)} 筆資料：\n\n" + "\n".join([str(r) for r in data])
        except Exception as e:
            reply = f"❌ 讀取 Google Sheet 發生錯誤：\n{e}"
        message = TextMessage(text=reply)
        line_bot_api.reply_message(event.reply_token, [message])
        return
    else:
        message = TextMessage(text="請輸入指令，例如 debug 來測試連線")
        line_bot_api.reply_message(event.reply_token, [message])

@app.route("/")
def home():
    return "✅ LINE Reminder Bot 正常運行中"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
