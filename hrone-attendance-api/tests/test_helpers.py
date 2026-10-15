from datetime import datetime, timezone
from app.main import IST, attendance_day, work_hours, late_minutes, overtime_minutes, shift_end_for_date

def test_late_grace_boundary():
    # Shift starts at 09:30 IST. Exactly 10 minutes is within grace.
    assert late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc), "2026-07-06", "09:30") == 0
    assert late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc), "2026-07-06", "09:30") == 10

def test_half_up_work_hours():
    start = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 6, 8, 30, 18, tzinfo=timezone.utc)
    assert work_hours(start, end) == 4.51

def test_overtime_minimum():
    emp_start = "09:30"
    assert overtime_minutes(datetime(2026,7,6,13,29,tzinfo=timezone.utc), "2026-07-06", emp_start, "18:30") == 0
    assert overtime_minutes(datetime(2026,7,6,13,30,tzinfo=timezone.utc), "2026-07-06", emp_start, "18:30") == 30

def test_overnight_attendance_day():
    emp = {"shift_start":"22:00","shift_end":"06:00"}
    punch = datetime(2026,7,7,0,29,tzinfo=timezone.utc)  # 05:59 IST
    assert attendance_day(punch, emp) == "2026-07-06"
