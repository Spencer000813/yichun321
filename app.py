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

# 檢查必要的環境變數
def check_environment_variables():
    required_vars = [
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_SECRET",
        "GOOGLE_CREDENTIALS_JSON",
        "GOOGLE_SPREADSHEET_ID"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ 缺少必要的環境變數: {', '.join(missing_vars)}")
        return False
    return True

# 檢查環境變數
if not check_environment_variables():
    print("請設定所有必要的環境變數後重新啟動")
    exit(1)

# LINE 機器人驗證資訊
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# 使用新版本的 LINE Bot API
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 授權和連接
def initialize_google_sheets():
    try:
        SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
        gc = gspread.authorize(credentials)
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        
        # 嘗試開啟試算表
        sheet = gc.open_by_key(spreadsheet_id).sheet1
        print(f"✅ 成功連接到 Google Sheets: {spreadsheet_id}")
        return gc, sheet
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Google Sheets 不存在或無法存取: {spreadsheet_id}")
        print("請檢查：")
        print("1. 試算表 ID 是否正確")
        print("2. 服務帳戶是否有存取權限")
        print("3. 試算表是否已共享給服務帳戶")
        return None, None
    except Exception as e:
        print(f"❌ Google Sheets 連接失敗: {e}")
        return None, None

# 初始化 Google Sheets
gc, sheet = initialize_google_sheets()

# 設定要發送早安訊息和週報的群組 ID
TARGET_GROUP_ID = os.getenv("MORNING_GROUP_ID", "C4e138aa0eb252daa89846daab0102e41")

@app.route("/")
def home():
    status = {
        "LINE Bot": "✅ 運行中",
        "Google Sheets": "✅ 已連接" if sheet else "❌ 連接失敗",
        "排程器": "✅ 運行中" if scheduler.running else "❌ 停止",
        "群組設定": "✅ 已設定" if TARGET_GROUP_ID != "C4e138aa0eb252daa89846daab0102e41" else "❌ 尚未設定"
    }
    
    status_text = "LINE Reminder Bot 狀態：\n\n"
    for key, value in status.items():
        status_text += f"{key}: {value}\n"
    
    return status_text.replace("\n", "<br>")

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 每天早上8:30發送早安訊息
def send_morning_message():
    try:
        if TARGET_GROUP_ID != "C4e138aa0eb252daa89846daab0102e41":
            message = TextMessage(text="早安，又是新的一天 ☀️")
            request = PushMessageRequest(
                to=TARGET_GROUP_ID,
                messages=[message]
            )
            line_bot_api.push_message(request)
            print(f"早安訊息已發送到群組: {TARGET_GROUP_ID}")
        else:
            print("推播群組 ID 尚未設定")
    except Exception as e:
        print(f"發送早安訊息失敗：{e}")

# 延遲倒數提醒
def send_countdown_reminder(user_id, minutes):
    try:
        message = TextMessage(text=f"⏰ {minutes}分鐘已到")
        request = PushMessageRequest(
            to=user_id,
            messages=[message]
        )
        line_bot_api.push_message(request)
        print(f"{minutes}分鐘倒數提醒已發送給：{user_id}")
    except Exception as e:
        print(f"推播{minutes}分鐘倒數提醒失敗：{e}")

# 每週日晚間推播下週行程
def weekly_summary():
    print("開始執行每週行程摘要...")
    
    # 檢查 Google Sheets 是否可用
    if not sheet:
        print("❌ Google Sheets 未連接，無法執行週報")
        return
    
    try:
        # 檢查是否已設定群組 ID
        if TARGET_GROUP_ID == "C4e138aa0eb252daa89846daab0102e41":
            print("週報群組 ID 尚未設定，跳過週報推播")
            return
            
        all_rows = sheet.get_all_values()[1:]
        now = datetime.now()
        
        # 計算下週一到下週日的範圍
        days_until_next_monday = (7 - now.weekday()) % 7
        if days_until_next_monday == 0:
            days_until_next_monday = 7
            
        start = now + timedelta(days=days_until_next_monday)
        end = start + timedelta(days=6)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        print(f"查詢時間範圍：{start.strftime('%Y/%m/%d %H:%M')} 到 {end.strftime('%Y/%m/%d %H:%M')}")
        
        user_schedules = {}

        for row in all_rows:
            if len(row) < 5:
                continue
            try:
                date_str, time_str, content, user_id, _ = row
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
                if start <= dt <= end:
                    user_schedules.setdefault(user_id, []).append((dt, content))
            except Exception as e:
                print(f"處理行程資料失敗：{e}")
                continue

        print(f"找到 {len(user_schedules)} 位使用者有下週行程")
        
        if not user_schedules:
            message_text = f"📅 下週行程摘要 ({start.strftime('%m/%d')} - {end.strftime('%m/%d')})：\n\n🎉 下週沒有安排任何行程，好好放鬆吧！"
        else:
            message_text = f"📅 下週行程摘要 ({start.strftime('%m/%d')} - {end.strftime('%m/%d')})：\n\n"
            
            all_schedules = []
            for user_id, items in user_schedules.items():
                for dt, content in items:
                    all_schedules.append((dt, content, user_id))
            
            all_schedules.sort()
            
            current_date = None
            for dt, content, user_id in all_schedules:
                if current_date != dt.date():
                    current_date = dt.date()
                    message_text += f"\n📆 *{dt.strftime('%m/%d (%a)')}*\n"
                
                message_text += f"• {dt.strftime('%H:%M')} {content}\n"
        
        try:
            message = TextMessage(text=message_text)
            request = PushMessageRequest(
                to=TARGET_GROUP_ID,
                messages=[message]
            )
            line_bot_api.push_message(request)
            print(f"已發送週報摘要到群組：{TARGET_GROUP_ID}")
        except Exception as e:
            print(f"推播週報到群組失敗：{e}")
                
        print("每週行程摘要執行完成")
                
    except Exception as e:
        print(f"每週行程摘要執行失敗：{e}")

# 手動觸發週報
def manual_weekly_summary():
    print("手動執行每週行程摘要...")
    weekly_summary()

# 排程任務
scheduler.add_job(
    weekly_summary, 
    CronTrigger(day_of_week="sun", hour=22, minute=0),
    id="weekly_summary"
)
scheduler.add_job(
    send_morning_message, 
    CronTrigger(hour=8, minute=30),
    id="morning_message"
)

# 指令對應表
EXACT_MATCHES = {
    "今日行程": "today",
    "明日行程": "tomorrow",
    "本週行程": "this_week",
    "下週行程": "next_week",
    "本月行程": "this_month",
    "下個月行程": "next_month",
    "明年行程": "next_year",
    "倒數計時": "countdown_3",
    "開始倒數": "countdown_3",
    "倒數3分鐘": "countdown_3",
    "倒數5分鐘": "countdown_5",
    "哈囉": "hello",
    "hi": "hi",
    "你還會說什麼?": "what_else"
}

# 檢查文字是否為行程格式
def is_schedule_format(text):
    """檢查文字是否像是行程格式"""
    parts = text.strip().split()
    if len(parts) < 2:
        return False
    
    try:
        date_part, time_part = parts[0], parts[1]
        
        if "/" in date_part:
            date_segments = date_part.split("/")
            if len(date_segments) == 2 or len(date_segments) == 3:
                if all(segment.isdigit() for segment in date_segments):
                    if ":" in time_part:
                        colon_index = time_part.find(":")
                        if colon_index > 0:
                            time_only = time_part[:colon_index+3]
                            if len(time_only) >= 4:
                                time_segments = time_only.split(":")
                                if len(time_segments) == 2:
                                    if all(segment.isdigit() for segment in time_segments):
                                        return True
    except:
        pass
    
    return False

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    lower_text = user_text.lower()
    user_id = getattr(event.source, "group_id", None) or event.source.user_id
    reply = None

    # 系統狀態檢查指令
    if lower_text == "系統狀態":
        status_info = []
        status_info.append("🤖 LINE Bot 系統狀態：")
        status_info.append(f"📊 Google Sheets: {'✅ 已連接' if sheet else '❌ 連接失敗'}")
        status_info.append(f"⏰ 排程器: {'✅ 運行中' if scheduler.running else '❌ 停止'}")
        status_info.append(f"👥 群組設定: {'✅ 已設定' if TARGET_GROUP_ID != 'C4e138aa0eb252daa89846daab0102e41' else '❌ 尚未設定'}")
        
        if not sheet:
            status_info.append("\n⚠️ Google Sheets 連接問題：")
            status_info.append("• 請檢查試算表 ID 是否正確")
            status_info.append("• 請檢查服務帳戶權限設定")
        
        reply = "\n".join(status_info)

    # 早安相關指令
    elif lower_text == "設定早安群組":
        group_id = getattr(event.source, "group_id", None)
        if group_id:
            global TARGET_GROUP_ID
            TARGET_GROUP_ID = group_id
            reply = f"✅ 已設定此群組為早安訊息群組\n群組 ID: {group_id}\n每天早上8:30會自動發送早安訊息"
        else:
            reply = "❌ 此指令只能在群組中使用"

    elif lower_text == "查看群組設定":
        reply = f"目前群組 ID: {TARGET_GROUP_ID}\n{'✅ 已設定' if TARGET_GROUP_ID != 'C4e138aa0eb252daa89846daab0102e41' else '❌ 尚未設定'}\n\n功能說明：\n• 早安訊息：每天8:30推播\n• 週報摘要：每週日晚上22:00推播下週行程"

    elif lower_text == "測試早安":
        group_id = getattr(event.source, "group_id", None)
        if group_id == TARGET_GROUP_ID or TARGET_GROUP_ID == "C4e138aa0eb252daa89846daab0102e41":
            reply = "早安，又是新的一天 ☀️"
        else:
            reply = "此群組未設定為推播群組"

    elif lower_text == "測試週報":
        if not sheet:
            reply = "❌ Google Sheets 未連接，無法執行週報"
        else:
            try:
                manual_weekly_summary()
                reply = "✅ 週報已手動執行，請檢查 log 確認執行狀況"
            except Exception as e:
                reply = f"❌ 週報執行失敗：{str(e)}"

    elif lower_text == "查看id":
        group_id = getattr(event.source, "group_id", None)
        user_id = event.source.user_id
        if group_id:
            reply = f"📋 目前資訊：\n群組 ID: {group_id}\n使用者 ID: {user_id}"
        else:
            reply = f"📋 目前資訊：\n使用者 ID: {user_id}\n（這是個人對話，沒有群組 ID）"

    elif lower_text == "查看排程":
        try:
            jobs = scheduler.get_jobs()
            if jobs:
                job_info = []
                for job in jobs:
                    next_run = job.next_run_time.strftime('%Y/%m/%d %H:%M:%S') if job.next_run_time else "未設定"
                    job_info.append(f"• {job.id}: {next_run}")
                reply = f"📋 目前排程工作：\n" + "\n".join(job_info)
            else:
                reply = "❌ 沒有找到任何排程工作"
        except Exception as e:
            reply = f"❌ 查看排程失敗：{str(e)}"

    elif lower_text == "如何增加行程":
        reply = (
            "📌 新增行程請使用以下格式：\n"
            "月/日 時:分 行程內容\n\n"
            "✅ 範例：\n"
            "7/1 14:00 餵小鳥\n"
            "（也可寫成 2025/7/1 14:00 客戶拜訪）\n\n"
            "⏰ 倒數計時功能：\n"
            "• 倒數3分鐘 / 倒數計時 / 開始倒數\n"
            "• 倒數5分鐘\n\n"
            "🌅 群組推播設定：\n"
            "• 設定早安群組 - 設定此群組為推播群組\n"
            "• 查看群組設定 - 查看目前設定\n"
            "• 測試早安 - 測試早安訊息\n\n"
            "🔧 測試指令：\n"
            "• 系統狀態 - 檢查系統運行狀態\n"
            "• 測試週報 - 手動執行週報推播\n"
            "• 查看排程 - 查看目前排程狀態\n"
            "• 查看id - 查看目前群組/使用者 ID"
        )

    else:
        reply_type = next((v for k, v in EXACT_MATCHES.items() if k.lower() == lower_text), None)

        if reply_type == "hello":
            reply = "怎樣?"
        elif reply_type == "hi":
            reply = "呷飽沒?"
        elif reply_type == "what_else":
            reply = "我愛你❤️"
        elif reply_type == "countdown_3":
            reply = "倒數計時3分鐘開始...\n（3分鐘後我會提醒你：3分鐘已到）"
            scheduler.add_job(
                send_countdown_reminder,
                trigger="date",
                run_date=datetime.now() + timedelta(minutes=3),
                args=[user_id, 3],
                id=f"countdown_3_{user_id}_{datetime.now().timestamp()}"
            )
        elif reply_type == "countdown_5":
            reply = "倒數計時5分鐘開始...\n（5分鐘後我會提醒你：5分鐘已到）"
            scheduler.add_job(
                send_countdown_reminder,
                trigger="date",
                run_date=datetime.now() + timedelta(minutes=5),
                args=[user_id, 5],
                id=f"countdown_5_{user_id}_{datetime.now().timestamp()}"
            )
        elif reply_type:
            if not sheet:
                reply = "❌ Google Sheets 未連接，無法查詢行程"
            else:
                reply = get_schedule(reply_type, user_id)
        else:
            # 檢查是否為行程格式
            if is_schedule_format(user_text):
                if not sheet:
                    reply = "❌ Google Sheets 未連接，無法新增行程"
                else:
                    reply = try_add_schedule(user_text, user_id)

    # 只有在 reply 不為 None 時才回應
    if reply:
        message = TextMessage(text=reply)
        line_bot_api.reply_message(
            event.reply_token,
            [message]
        )

def get_schedule(period, user_id):
    try:
        all_rows = sheet.get_all_values()[1:]
        now = datetime.now()
        schedules = []

        for row in all_rows:
            if len(row) < 5:
                continue
            try:
                date_str, time_str, content, uid, _ = row
                dt = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y/%m/%d %H:%M")
            except Exception as e:
                print(f"解析時間失敗：{e}")
                continue

            if user_id.lower() != uid.lower():
                continue

            if (
                (period == "today" and dt.date() == now.date()) or
                (period == "tomorrow" and dt.date() == (now + timedelta(days=1)).date()) or
                (period == "this_week" and dt.isocalendar()[1] == now.isocalendar()[1] and dt.year == now.year) or
                (period == "next_week" and dt.isocalendar()[1] == (now + timedelta(days=7)).isocalendar()[1] and dt.year == (now + timedelta(days=7)).year) or
                (period == "this_month" and dt.year == now.year and dt.month == now.month) or
                (period == "next_month" and (
                    dt.year == (now.year + 1 if now.month == 12 else now.year)
                ) and dt.month == ((now.month % 12) + 1)) or
                (period == "next_year" and dt.year == now.year + 1)
            ):
                schedules.append(f"*{dt.strftime('%Y/%m/%d %H:%M')}*\n{content}")

        return "\n\n".join(schedules) if schedules else "目前沒有相關排程。"
    except Exception as e:
        print(f"取得行程失敗：{e}")
        return "取得行程時發生錯誤，請稍後再試。"

def try_add_schedule(text, user_id):
    try:
        parts = text.strip().split()
        if len(parts) >= 2:
            date_part = parts[0]
            time_and_content = " ".join(parts[1:])
            
            time_part = None
            content = None
            
            if ":" in time_and_content:
                colon_index = time_and_content.find(":")
                if colon_index >= 1:
                    time_start = max(0, colon_index - 2)
                    while time_start < colon_index and not time_and_content[time_start].isdigit():
                        time_start += 1
                    
                    time_end = colon_index + 3
                    if time_end <= len(time_and_content):
                        potential_time = time_and_content[time_start:time_end]
                        if ":" in potential_time:
                            time_segments = potential_time.split(":")
                            if len(time_segments) == 2 and all(seg.isdigit() for seg in time_segments):
                                time_part = potential_time
                                content = time_and_content[time_end:].strip()
                                
                                if not content:
                                    content = time_and_content[time_end:].strip()
            
            if not time_part or not content:
                return "❌ 時間格式錯誤，請使用：月/日 時:分 行程內容\n範例：7/1 14:00 開會"
            
            if date_part.count("/") == 1:
                date_part = f"{datetime.now().year}/{date_part}"
            
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y/%m/%d %H:%M")
            
            if dt < datetime.now():
                return "❌ 不能新增過去的時間，請確認日期和時間是否正確。"
            
            sheet.append_row([
                dt.strftime("%Y/%m/%d"),
                dt.strftime("%H:%M"),
                content,
                user_id,
                ""
            ])
            return (
                f"✅ 行程已新增：\n"
                f"- 日期：{dt.strftime('%Y/%m/%d')}\n"
                f"- 時間：{dt.strftime('%H:%M')}\n"
                f"- 內容：{content}\n"
                f"（一小時前會提醒你）"
            )
    except ValueError as e:
        print(f"時間格式錯誤：{e}")
        return "❌ 時間格式錯誤，請使用：月/日 時:分 行程內容\n範例：7/1 14:00 開會"
    except Exception as e:
        print(f"新增行程失敗：{e}")
        return "❌ 新增行程失敗，請稍後再試或聯絡管理員。"
    
    return None

if __name__ == "__main__":
    print("LINE Bot 啟動中...")
    print("環境變數檢查：✅")
    print(f"Google Sheets 連接：{'✅' if sheet else '❌'}")
    print("排程任務:")
    print("- 每天早上 8:30 發送早安訊息")
    print("- 每週日晚上 22:00 發送下週行程摘要")
    print("倒數計時功能:")
    print("- 倒數3分鐘：輸入 '倒數3分鐘' 或 '倒數計時' 或 '開始倒數'")
    print("- 倒數5分鐘：輸入 '倒數5分鐘'")
    
    # 顯示目前排程狀態
    try:
        jobs = scheduler.get_jobs()
        print(f"已載入 {len(jobs)} 個排程工作")
        for job in jobs:
            next_run = job.next_run_time.strftime('%Y/%m/%d %H:%M:%S') if job.next_run_time else "未設定"
            print(f"  - {job.id}: 下次執行時間 {next_run}")
    except Exception as e:
        print(f"查看排程狀態失敗：{e}")
    
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
