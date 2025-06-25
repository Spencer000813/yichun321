# app.py
import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import atexit
from schedule_manager import ScheduleManager
from message_handler import process_message

TZ = pytz.timezone('Asia/Taipei')
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("請設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
schedule_manager = ScheduleManager()

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    # 處理倒數指令
    if text.startswith("倒數") and "分鐘" in text:
        import re
        from threading import Timer
        try:
            minute = int(re.search(r'\d+', text).group())
            if 0 < minute <= 60:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"倒數 {minute} 分鐘開始！我會在時間到時提醒你。")
                )
                target_id = event.source.group_id if hasattr(event.source, 'group_id') and event.source.group_id else user_id
                def notify():
                    try:
                        line_bot_api.push_message(
                            target_id,
                            TextSendMessage(text=f"⏰ {minute} 分鐘倒數結束，時間到囉！")
                        )
                    except Exception as e:
                        print(f"推送提醒失敗: {e}")
                Timer(minute * 60, notify).start()
                return
            else:
                reply_text = "倒數時間請設定在 1-60 分鐘之間"
        except:
            reply_text = "請輸入正確格式：倒數 5 分鐘"
    else:
        reply_text = process_message(text, user_id, schedule_manager)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def friday_reminder():
    try:
        all_users = schedule_manager.get_two_weeks_later_schedules()
        for uid, schedules in all_users.items():
            if schedules:
                msg = "🔔 兩週後行程提醒\n\n"
                for s in sorted(schedules, key=lambda x: (x['日期'], x.get('時間', ''))):
                    date = s.get('日期', '')
                    time = s.get('時間', '') or '全天'
                    content = s.get('行程內容', '')
                    msg += f"📅 {date} {time}\n📝 {content}\n\n"
                try:
                    line_bot_api.push_message(uid, TextSendMessage(text=msg.strip()))
                    print(f"推播成功: {uid}")
                except Exception as e:
                    print(f"推播失敗 {uid}: {e}")
        print(f"週五提醒完成，共 {len(all_users)} 位")
    except Exception as e:
        print(f"週五提醒錯誤: {e}")

scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(friday_reminder, 'cron', day_of_week='fri', hour=10, minute=0, id='weekly_reminder')
scheduler.start()

atexit.register(lambda: scheduler.shutdown())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
