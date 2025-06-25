# schedule_manager.py
import json
import os
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pytz

class ScheduleManager:
    def __init__(self):
        credentials_info = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        gc = gspread.authorize(credentials)
        spreadsheet_id = os.getenv("SPREADSHEET_ID")
        self.sheet = gc.open_by_key(spreadsheet_id).sheet1
        self.timezone = pytz.timezone("Asia/Taipei")

    def add_schedule(self, user_id, date, content, time=None):
        now = datetime.now(self.timezone).strftime('%Y-%m-%d %H:%M:%S')
        self.sheet.append_row([user_id, date, time or '', content, now])

    def get_schedules_by_date(self, user_id, target_date):
        records = self.sheet.get_all_records()
        return [row for row in records if row["使用者ID"] == user_id and row["日期"] == target_date]

    def get_two_weeks_later_schedules(self):
        records = self.sheet.get_all_records()
        target_date = (datetime.now(self.timezone) + timedelta(days=14)).strftime("%Y-%m-%d")
        results = {}
        for row in records:
            if row["日期"] == target_date:
                uid = row["使用者ID"]
                if uid not in results:
                    results[uid] = []
                results[uid].append(row)
        return results
