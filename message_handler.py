# message_handler.py
from datetime import datetime
import re

def process_message(text, user_id, manager):
    today = datetime.now(manager.timezone).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(manager.timezone) + manager.timezone.utcoffset(datetime.now())).strftime("%Y-%m-%d")

    if text == "今天有哪些行程":
        data = manager.get_schedules_by_date(user_id, today)
    elif text == "明天有哪些行程":
        data = manager.get_schedules_by_date(user_id, tomorrow)
    else:
        # 自動新增行程（格式：「6月30號 下午2點 聚會」或「7/1 看電影」）
        date_match = re.search(r'(\d{1,2})月(\d{1,2})[日號]?', text)
        if not date_match:
            date_match = re.search(r'(\d{1,2})/(\d{1,2})', text)
        if date_match:
            month, day = date_match.groups()
            year = datetime.now().year
            date_str = f"{year}-{int(month):02d}-{int(day):02d}"
            time_match = re.search(r'(上午|下午)?(\d{1,2})點', text)
            hour = None
            if time_match:
                period, h = time_match.groups()
                hour = int(h)
                if period == '下午' and hour < 12:
                    hour += 12
                time_str = f"{hour:02d}:00"
            else:
                time_str = ""
            content = re.sub(r'.*?(號|日|\d{1,2}/\d{1,2})', '', text).strip()
            if not content:
                return "請輸入行程內容，例如：7月1日 下午3點 開會"
            manager.add_schedule(user_id, date_str, content, time_str)
            return f"✅ 已加入行程：{date_str} {time_str or '全天'} {content}"
        return "請輸入有效指令，或使用：今天有哪些行程 / 明天有哪些行程"

    if not data:
        return "🔍 查無行程"
    response = ""
    for d in data:
        date = d.get("日期", "")
        time = d.get("時間", "") or "全天"
        content = d.get("行程內容", "")
        response += f"📅 {date} {time}\n📝 {content}\n\n"
    return response.strip()
