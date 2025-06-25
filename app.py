import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# LINE Bot 驗證資料（從環境變數中取得）
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

# 訊息處理邏輯：支援「倒數 X 分鐘」
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if text.startswith("倒數") and "分鐘" in text:
        try:
            minute = int(text.replace("倒數", "").replace("分鐘", "").strip())
            if 0 < minute <= 60:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"倒數 {minute} 分鐘開始！我會在時間到時提醒你。")
                )

                # 計算剩餘時間後自動推送訊息（非即時，可改用排程強化）
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

    # 如果格式錯誤就提醒
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請輸入格式：倒數 X 分鐘，例如：倒數 5 分鐘")
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

