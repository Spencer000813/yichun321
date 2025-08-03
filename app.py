import os
import json
import random
from datetime import datetime, timedelta
from flask import Flask, request, abort

import gspread
from google.oauth2.service_account import Credentials

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 初始化 Flask 與 APScheduler
app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# LINE 機器人驗證資訊
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 授權
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
credentials = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(credentials)
spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
sheet = gc.open_by_key(spreadsheet_id).sheet1

# 設定要發送行程預覽的群組 ID
TARGET_GROUP_ID = os.getenv("SCHEDULE_GROUP_ID", "C4e138aa0eb252daa89846daab0102e41")  # 將「你的群組ID」替換成實際的群組ID

# 撲克牌遊戲類
class PokerGame:
    def __init__(self):
        # 建立54張牌的牌組（包含鬼牌）
        self.suits = ['♠', '♥', '♦', '♣']  # 黑桃、紅心、方塊、梅花
        self.ranks = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
        self.jokers = ['鬼牌1', '鬼牌2']  # 兩張鬼牌
    
    def create_deck(self):
        """建立完整的54張牌組"""
        deck = []
        
        # 添加52張一般牌
        for suit in self.suits:
            for rank in self.ranks:
                deck.append(f"{suit}{rank}")
        
        # 添加2張鬼牌
        deck.extend(self.jokers)
        
        return deck
    
    def draw_cards(self, num_cards=5):
        """隨機抽取指定數量的牌（不重複）"""
        deck = self.create_deck()
        random.shuffle(deck)
        # 使用 random.sample 確保抽取的牌不會重複
        if num_cards > len(deck):
            num_cards = len(deck)  # 防止抽取數量超過牌組總數
        return random.sample(deck, num_cards)
    
    def get_card_display(self, card):
        """美化牌的顯示格式"""
        if card in self.jokers:
            return "🃏鬼牌"  # 統一顯示鬼牌
        
        # 直接返回原始牌面（紅心♥和方塊♦符號會自動顯示為紅色）
        return card

# 實例化撲克牌遊戲
poker_game = PokerGame()

def handle_poker_draw(user_id):
    """處理撲克牌抽牌"""
    try:
        # 抽取5張牌（確保不重複）
        drawn_cards = poker_game.draw_cards(5)
        
        # 美化牌面顯示
        card_display = []
        for i, card in enumerate(drawn_cards, 1):
            display_card = poker_game.get_card_display(card)
            card_display.append(f"{i}. {display_card}")
        
        # 組合回覆訊息
        reply = (
            f"🎴 撲克牌抽牌結果\n"
            f"====================\n"
            f"🕐 抽牌時間：{datetime.now().strftime('%H:%M')}\n"
            f"🎯 抽牌結果：\n\n"
            + "\n".join(card_display) + "\n\n"
            f"====================\n"
            f"🎴 抽牌完成！"
        )
        
        return reply
        
    except Exception as e:
        print(f"撲克牌遊戲錯誤：{e}")
        return (
            "😵 撲克牌遊戲出了點小狀況\n"
            "====================\n"
            "🔧 請稍後再試試看"
        )

@app.route("/")
def home():
    return "LINE Reminder Bot is running."

@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 發送功能說明
def send_help_message():
    return (
        "🤖 LINE 行程助理使用說明\n"
        "==============\n\n"
        "📌 新增行程格式：\n"
        "月/日 時:分 行程內容\n\n"
        "✅ 範例：\n"
        "• 7/1 14:00 餵小鳥\n"
        "• 2025/7/1 14:00 客戶拜訪\n\n"
        "📋 查詢行程指令：\n"
        "• 今日行程 - 查看今天的所有行程\n"
        "• 明日行程 - 查看明天的所有行程\n"
        "• 本週行程 - 查看本週的所有行程\n"
        "• 下週行程 - 查看下週的所有行程\n"
        "• 本月行程 - 查看本月的所有行程\n"
        "• 下個月行程 - 查看下個月的所有行程\n"
        "• 明年行程 - 查看明年的所有行程\n\n"
        "🎴 撲克牌遊戲：\n"
        "• 出牌 - 隨機抽取5張撲克牌\n\n"
        "⏰ 倒數計時功能：\n"
        "• 倒數3分鐘 / 倒數計時 / 開始倒數\n"
        "• 倒數5分鐘\n\n"
        "📊 群組推播設定：\n"
        "• 設定推播群組 - 設定此群組為推播群組\n"
        "• 查看群組設定 - 查看目前設定\n"
        "• 功能說明 - 顯示此說明訊息\n\n"
        "🔧 測試指令：\n"
        "• 測試行程預覽 - 手動執行2週後行程預覽\n"
        "• 查看排程 - 查看目前排程狀態\n"
        "• 查看id - 查看目前群組/使用者 ID\n\n"
        "📅 自動推播：\n"
        "每週五早上10:00自動推播2週後行程預覽"
    )

# 延遲三分鐘後推播倒數訊息
def send_countdown_reminder(user_id, minutes):
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=f"⏰ {minutes}分鐘已到"))
        print(f"{minutes}分鐘倒數提醒已發送給：{user_id}")
    except Exception as e:
        print(f"推播{minutes}分鐘倒數提醒失敗：{e}")

# 修改為每週五早上推播2週後行程
def weekly_summary():
    print("開始執行2週後行程摘要...")
    try:
        # 檢查是否已設定群組 ID
        if TARGET_GROUP_ID == "C4e138aa0eb252daa89846daab0102e41":
            print("週報群組 ID 尚未設定，跳過週報推播")
            return
            
        all_rows = sheet.get_all_values()[1:]
        now = datetime.now()
        
        # 計算2週後的時間範圍
        # 從今天起算2週後的週一到週日
        start = now + timedelta(weeks=2)
        
        # 找到那一週的週一
        days_until_monday = (7 - start.weekday()) % 7
        if days_until_monday == 0 and start.weekday() != 0:  # 如果不是週一
            days_until_monday = 7
        elif start.weekday() == 0:  # 如果已經是週一
            days_until_monday = 0
        else:
            days_until_monday = 7 - start.weekday()
            
        start = start + timedelta(days=days_until_monday)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 該週的週日
        end = start + timedelta(days=6)
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        print(f"查詢2週後行程時間範圍：{start.strftime('%Y/%m/%d %H:%M')} 到 {end.strftime('%Y/%m/%d %H:%M')}")
        
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

        print(f"找到 {len(user_schedules)} 位使用者有2週後行程")
        
        if not user_schedules:
            # 如果沒有行程，也發送提醒
            message = f"📅 2週後行程預覽 ({start.strftime('%m/%d')} - {end.strftime('%m/%d')})：\n\n🎉 2週後沒有安排任何行程，目前行程安排很輕鬆！"
        else:
            # 整理所有使用者的行程到一個訊息中
            message = f"📅 2週後行程預覽 ({start.strftime('%m/%d')} - {end.strftime('%m/%d')})：\n\n"
            
            # 按日期排序所有行程
            all_schedules = []
            for user_id, items in user_schedules.items():
                for dt, content in items:
                    all_schedules.append((dt, content, user_id))
            
            all_schedules.sort()  # 按時間排序
            
            current_date = None
            for dt, content, user_id in all_schedules:
                # 如果是新的日期，加上日期標題
                if current_date != dt.date():
                    current_date = dt.date()
                    message += f"\n📆 *{dt.strftime('%m/%d (%a)')}*\n"
                
                # 顯示時間和內容
                message += f"• {dt.strftime('%H:%M')} {content}\n"
        
        try:
            line_bot_api.push_message(TARGET_GROUP_ID, TextSendMessage(text=message))
            print(f"已發送2週後行程預覽到群組：{TARGET_GROUP_ID}")
        except Exception as e:
            print(f"推播2週後行程到群組失敗：{e}")
                
        print("2週後行程預覽執行完成")
                
    except Exception as e:
        print(f"2週後行程預覽執行失敗：{e}")

# 手動觸發週報（用於測試）
def manual_weekly_summary():
    print("手動執行2週後行程預覽...")
    weekly_summary()

# 修改排程任務 - 移除早安訊息
scheduler.add_job(
    weekly_summary, 
    CronTrigger(day_of_week="fri", hour=10, minute=0),   # 週五早上 10:00
    id="weekly_summary"
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
    "出牌": "poker_draw",
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
    
    # 檢查前兩個部分是否像日期時間格式
    try:
        date_part, time_part = parts[0], parts[1]
        
        # 檢查日期格式 (M/D 或 YYYY/M/D)
        if "/" in date_part:
            date_segments = date_part.split("/")
            if len(date_segments) == 2 or len(date_segments) == 3:
                # 檢查是否都是數字
                if all(segment.isdigit() for segment in date_segments):
                    # 檢查時間格式 (HH:MM)，但允許沒有空格的情況
                    if ":" in time_part:
                        # 找到冒號的位置，提取時間部分
                        colon_index = time_part.find(":")
                        if colon_index > 0:
                            # 提取時間部分（HH:MM）
                            time_only = time_part[:colon_index+3]  # 包含HH:MM
                            if len(time_only) >= 4:  # 至少要有H:MM或HH:M
                                time_segments = time_only.split(":")
                                if len(time_segments) == 2:
                                    if all(segment.isdigit() for segment in time_segments):
                                        return True
    except:
        pass
    
    return False

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    lower_text = user_text.lower()
    user_id = getattr(event.source, "group_id", None) or event.source.user_id
    reply = None  # 預設不回應

    # 指令處理
    if lower_text == "設定推播群組":
        group_id = getattr(event.source, "group_id", None)
        if group_id:
            global TARGET_GROUP_ID
            TARGET_GROUP_ID = group_id
            reply = f"✅ 已設定此群組為行程推播群組\n📱 群組 ID: {group_id}\n📅 每週五早上10:00會自動推播2週後行程預覽"
        else:
            reply = "❌ 此指令只能在群組中使用"
    elif lower_text == "查看群組設定":
        reply = f"📱 目前群組 ID: {TARGET_GROUP_ID}\n{'✅ 已設定推播群組' if TARGET_GROUP_ID != 'C4e138aa0eb252daa89846daab0102e41' else '❌ 尚未設定推播群組'}\n\n📅 自動推播功能：\n每週五早上10:00推播2週後行程預覽"
    elif lower_text == "功能說明" or lower_text == "說明" or lower_text == "help":
        reply = send_help_message()
    elif lower_text == "測試行程預覽":
        try:
            manual_weekly_summary()
            reply = "✅ 2週後行程預覽已手動執行，請檢查 log 確認執行狀況"
        except Exception as e:
            reply = f"❌ 2週後行程預覽執行失敗：{str(e)}"
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
        reply = send_help_message()
    else:
        reply_type = next((v for k, v in EXACT_MATCHES.items() if k.lower() == lower_text), None)

        if reply_type == "hello":
            reply = "怎樣?"
        elif reply_type == "hi":
            reply = "呷飽沒?"
        elif reply_type == "what_else":
            reply = "我愛你❤️"
        elif reply_type == "poker_draw":
            reply = handle_poker_draw(user_id)
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
            reply = get_schedule(reply_type, user_id)
        else:
            # 檢查是否為行程格式
            if is_schedule_format(user_text):
                reply = try_add_schedule(user_text, user_id)
            # 如果不是行程格式，就不回應（reply 保持 None）

    # 只有在 reply 不為 None 時才回應
    if reply:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def get_schedule(period, user_id):
    try:
        all_rows = sheet.get_all_values()[1:]
        now = datetime.now()
        schedules = []

        # 定義期間名稱
        period_names = {
            "today": "今日行程",
            "tomorrow": "明日行程", 
            "this_week": "本週行程",
            "next_week": "下週行程",
            "this_month": "本月行程",
            "next_month": "下個月行程",
            "next_year": "明年行程"
        }

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
                schedules.append((dt, content))

        if not schedules:
            return f"📅 {period_names.get(period, '行程')}：\n\n🎉 目前沒有安排任何行程"

        # 按時間排序
        schedules.sort()
        
        # 格式化輸出
        result = f"📅 {period_names.get(period, '行程')}：\n{'═' * 20}\n\n"
        
        current_date = None
        for dt, content in schedules:
            # 如果是新的日期，加上日期標題
            if current_date != dt.date():
                current_date = dt.date()
                if len(schedules) > 1 and period in ["this_week", "next_week", "this_month", "next_month", "next_year"]:
                    result += f"📆 {dt.strftime('%m/%d (%a)')}\n"
                    result += f"{'─' * 15}\n"
            
            # 顯示時間和內容
            result += f"🕐 {dt.strftime('%H:%M')} │ {content}\n"
            
            # 在多日期顯示時添加空行
            if len(schedules) > 1 and period in ["this_week", "next_week", "this_month", "next_month", "next_year"]:
                # 檢查下一個行程是否是不同日期
                current_index = schedules.index((dt, content))
                if current_index < len(schedules) - 1:
                    next_dt, _ = schedules[current_index + 1]
                    if next_dt.date() != dt.date():
                        result += "\n"

        return result.rstrip()
        
    except Exception as e:
        print(f"取得行程失敗：{e}")
        return "❌ 取得行程時發生錯誤，請稍後再試。"

def try_add_schedule(text, user_id):
    try:
        parts = text.strip().split()
        if len(parts) >= 2:
            date_part = parts[0]
            time_and_content = " ".join(parts[1:])
            
            # 處理時間和內容可能沒有空格分隔的情況
            # 例如: "7/1 14:00餵小鳥" 或 "7/1 14:00 餵小鳥"
            time_part = None
            content = None
            
            # 尋找時間格式 HH:MM
            if ":" in time_and_content:
                colon_index = time_and_content.find(":")
                # 假設時間格式是 HH:MM，所以從冒號往前1-2位和往後2位
                if colon_index >= 1:
                    # 找到時間的開始位置
                    time_start = max(0, colon_index - 2)
                    while time_start < colon_index and not time_and_content[time_start].isdigit():
                        time_start += 1
                    
                    # 找到時間的結束位置（冒號後2位數字）
                    time_end = colon_index + 3
                    if time_end <= len(time_and_content):
                        potential_time = time_and_content[time_start:time_end]
                        # 驗證時間格式
                        if ":" in potential_time:
                            time_segments = potential_time.split(":")
                            if len(time_segments) == 2 and all(seg.isdigit() for seg in time_segments):
                                time_part = potential_time
                                content = time_and_content[time_end:].strip()
                                
                                # 如果沒有內容，可能是因為時間和內容之間沒有空格
                                if not content:
                                    content = time_and_content[time_end:].strip()
            
            # 如果無法解析時間，返回格式錯誤
            if not time_part or not content:
                return "❌ 時間格式錯誤，請使用：月/日 時:分 行程內容\n範例：7/1 14:00 開會"
            
            # 如果日期格式是 M/D，自動加上當前年份
            if date_part.count("/") == 1:
                date_part = f"{datetime.now().year}/{date_part}"
            
            dt = datetime.strptime(f"{date_part} {time_part}", "%Y/%m/%d %H:%M")
            
            # 檢查日期是否為過去時間
            if dt < datetime.now():
                return "❌ 不能新增過去的時間，請確認日期和時間是否正確。"
            
            # 只新增主要行程，移除提醒行程
            sheet.append_row([
                dt.strftime("%Y/%m/%d"),
                dt.strftime("%H:%M"),
                content,
                user_id,
                ""
            ])
            return (
                f"✅ 行程新增成功！\n"
                f"{'═' * 20}\n"
                f"📅 日期：{dt.strftime('%Y/%m/%d (%a)')}\n"
                f"🕐 時間：{dt.strftime('%H:%M')}\n"
                f"📝 內容：{content}\n"
                f"{'─' * 20}\n"
                f"📝 行程已成功記錄"
            )
    except ValueError as e:
        print(f"時間格式錯誤：{e}")
        return "❌ 時間格式錯誤，請使用：月/日 時:分 行程內容\n範例：7/1 14:00 開會"
    except Exception as e:
        print(f"新增行程失敗：{e}")
        return "❌ 新增行程失敗，請稍後再試或聯絡管理員。"
    
    return None

if __name__ == "__main__":
    print("🤖 LINE 行程助理啟動中...")
    print("==============")
    print("📅 排程任務:")
    print("   • 每週五早上 10:00 發送2週後行程預覽")
    print("🎴 撲克牌遊戲:")
    print("   • 輸入 '出牌' 隨機抽取5張撲克牌")
    print("⏰ 倒數計時功能:")
    print("   • 倒數3分鐘：輸入 '倒數3分鐘' 或 '倒數計時' 或 '開始倒數'")
    print("   • 倒數5分鐘：輸入 '倒數5分鐘'")
    print("💡 輸入 '功能說明' 查看完整功能列表")
    
    # 顯示目前排程狀態
    try:
        jobs = scheduler.get_jobs()
        print(f"✅ 已載入 {len(jobs)} 個排程工作")
        for job in jobs:
            next_run = job.next_run_time.strftime('%Y/%m/%d %H:%M:%S') if job.next_run_time else "未設定"
            print(f"   • {job.id}: 下次執行時間 {next_run}")
    except Exception as e:
        print(f"❌ 查看排程狀態失敗：{e}")
    
    print("==============")
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
