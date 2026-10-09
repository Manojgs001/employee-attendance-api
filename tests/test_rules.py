"""
Unit tests for Business Rules R1 through R8 and core logic.
"""
from datetime import datetime, timezone, timedelta, date as dt_date
from decimal import Decimal
import pytest

from app.main import (
    round_half_up,
    calculate_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    count_working_days,
    IST,
)


def test_r8_rounding():
    """Test R8: 2 decimals half-up for numbers, 4 decimals half-up for rates."""
    assert round_half_up(2.555, 2) == 2.56
    assert round_half_up(2.554, 2) == 2.55
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(9.116666, 2) == 9.12
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234


def test_r1_attendance_date_normal_shift():
    """R1: Standard shift 09:30 - 18:30."""
    # Punch in 09:28 IST on 2026-07-06 (03:58 UTC)
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = calculate_attendance_date(dt_utc, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    """
    R1: Overnight shift 22:00 - 06:00.
    A punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    # 1. Punch in 21:55 IST on 2026-07-06 (16:25 UTC) -> belongs to 2026-07-06
    dt_utc1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert calculate_attendance_date(dt_utc1, "22:00", "06:00") == "2026-07-06"

    # 2. Punch in 01:15 IST on 2026-07-07 (19:45 UTC July 6) -> earlier than 06:00, belongs to 2026-07-06
    dt_utc2 = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    assert calculate_attendance_date(dt_utc2, "22:00", "06:00") == "2026-07-06"

    # 3. Punch in 06:00 IST on 2026-07-07 -> not strictly earlier than 06:00 -> belongs to 2026-07-07
    dt_utc3 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    assert calculate_attendance_date(dt_utc3, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes():
    """
    R2: Late. Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start.
    Shift 09:30:
    09:40:00 -> 0
    09:40:01 -> 10
    10:15:59 -> 45
    """
    shift_start = "09:30"
    att_date = "2026-07-06"

    # 09:28:00 IST (Early)
    dt1 = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    assert compute_late_minutes(dt1, shift_start, att_date) == 0

    # 09:40:00 IST (Exactly 10 min grace)
    dt2 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(dt2, shift_start, att_date) == 0

    # 09:40:01 IST (Strictly more than 10 min -> 10 min late)
    dt3 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(dt3, shift_start, att_date) == 10

    # 10:15:59 IST (45 min 59 sec -> 45 min late)
    dt4 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(dt4, shift_start, att_date) == 45


def test_r3_overtime():
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    shift_start = "09:30"
    shift_end = "18:30"
    att_date = "2026-07-06"

    # 18:35 IST (5 minutes overtime -> under 30 -> 0)
    dt1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(dt1, shift_start, shift_end, att_date) == 0

    # 18:59:59 IST (29 min 59 sec -> under 30 -> 0)
    dt2 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(dt2, shift_start, shift_end, att_date) == 0

    # 19:00:00 IST (Exactly 30 min -> 30)
    dt3 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(dt3, shift_start, shift_end, att_date) == 30

    # 19:10:45 IST (40 min 45 sec -> 40)
    dt4 = datetime(2026, 7, 6, 19, 10, 45, tzinfo=IST)
    assert compute_overtime(dt4, shift_start, shift_end, att_date) == 40


def test_r3_overtime_overnight_shift():
    """R3: Overtime on overnight shift (shift_end on next calendar day)."""
    shift_start = "22:00"
    shift_end = "06:00"
    att_date = "2026-07-06"

    # Punched out 2026-07-07 at 06:40 IST (40 minutes overtime)
    dt = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(dt, shift_start, shift_end, att_date) == 40


def test_r4_work_hours():
    """R4: Work hours = (punch_out - punch_in) in sec / 3600, rounded 2 decimals half-up."""
    # Sample 1: 03:58:00 UTC to 13:05:00 UTC = 32820 seconds -> 9.116666... -> 9.12
    dt_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    dt_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(dt_in, dt_out) == 9.12

    # Sample 4: 3.5 hours
    dt_in4 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    dt_out4 = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)
    assert compute_work_hours(dt_in4, dt_out4) == 3.5


def test_r5_half_day():
    """R5: Half day is true when work_hours < 4.50."""
    assert compute_half_day(3.5) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(9.12) is False
    assert compute_half_day(None) is False


def test_r7_working_days():
    """R7: Working days are Mon-Fri, counted from joined_on for mid-month joiners."""
    # July 2026: July 1 is Wednesday. July 31 is Friday.
    # Total weekdays in July 2026: 23 weekdays.
    assert count_working_days("2026-07-01", "2026-07-31") == 23

    # Mid-month joiner on 2026-07-15 (Wednesday):
    # July 15 to July 31 weekdays:
    # Week of July 13: Wed(15), Thu(16), Fri(17) = 3
    # Week of July 20: 5
    # Week of July 27: 5
    # Total = 13 weekdays
    assert count_working_days("2026-07-15", "2026-07-31") == 13

    # Employee joined after month end:
    assert count_working_days("2026-08-01", "2026-07-31") == 0
