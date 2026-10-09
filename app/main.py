"""
Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (read from environment, .env loaded if present)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal | int, decimals: int) -> float:
    """R8: Rounding of reported numbers half-up (rates: 4 decimals, numbers: 2 decimals)."""
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime (BSON date) to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt_truncated(epoch_ms: int) -> tuple[datetime, int]:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and the truncated value is stored and returned.
    Returns (UTC datetime truncated to seconds, truncated epoch ms).
    """
    truncated_sec = epoch_ms // 1000
    truncated_ms = truncated_sec * 1000
    dt_utc = datetime.fromtimestamp(truncated_sec, tz=timezone.utc)
    return dt_utc, truncated_ms


def parse_time_str(time_str: str) -> tuple[int, int]:
    """Parses 'HH:MM' string to (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: Overnight shift when shift_end <= shift_start."""
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)
    return (eh, em) <= (sh, sm)


def calculate_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    eh, em = parse_time_str(shift_end)

    if is_overnight_shift(shift_start, shift_end):
        if dt_ist.time() < time(eh, em):
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Late. Grace period 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start,
    not since grace period ended.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    sh, sm = parse_time_str(shift_start)
    att_date = dt_date.fromisoformat(attendance_date)
    start_dt_ist = datetime.combine(att_date, time(sh, sm), tzinfo=IST)

    diff_sec = (dt_ist - start_dt_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    For overnight shift, shift_end falls on the calendar day after attendance_date.
    """
    dt_ist = punch_out_dt.astimezone(IST)
    eh, em = parse_time_str(shift_end)
    att_date = dt_date.fromisoformat(attendance_date)

    if is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    end_dt_ist = datetime.combine(end_date, time(eh, em), tzinfo=IST)
    diff_sec = (dt_ist - end_dt_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day = true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Monday - Friday days between start_date and end_date inclusive."""
    d_start = dt_date.fromisoformat(start_date_str)
    d_end = dt_date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    count = 0
    while cur <= d_end:
        if cur.weekday() < 5:  # Mon=0..Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an attendance document from MongoDB for the API response."""
    history_formatted = []
    for h in doc.get("history", []):
        entry: Dict[str, Any] = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for k, change in h.get("changes", {}).items():
            from_v = change.get("from")
            to_v = change.get("to")
            if k in ("punch_in", "punch_out"):
                entry["changes"][k] = {
                    "from": dt_to_epoch_ms(from_v) if isinstance(from_v, datetime) else from_v,
                    "to": dt_to_epoch_ms(to_v) if isinstance(to_v, datetime) else to_v,
                }
            else:
                entry["changes"][k] = {"from": from_v, "to": to_v}
        history_formatted.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_formatted,
    }


def format_employee_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an employee document from MongoDB for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Lifespan: Create Indexes at Startup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
        db.attendance_logs.create_index([("date", 1)])
    except Exception:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, ABSENT, LEAVE, WFH, or ON_DUTY")
        return v


# --------------------------------------------------------------------------- #
# System Endpoint
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Creates a new employee record. Enforces unique emp_code and differing shift times."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    # Check for existing employee before insert
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_record(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists employees sorted by emp_code ascending. Total reflects filters."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Records a punch-in event. Concurrency-safe via unique index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    dt_utc, _ = epoch_ms_to_dt_truncated(now_ms)

    att_date = calculate_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(dt_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": att_date}):
        raise HTTPException(status_code=409, detail="already punched in for this date")

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Closes the most recent open attendance record for the employee at or before punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    now_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_out_dt, _ = epoch_ms_to_dt_truncated(now_ms)

    # Find the most recent record whose punch_in is <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for this employee")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    sec_diff = (punch_out_dt - punch_in_dt).total_seconds()
    if sec_diff <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if sec_diff > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if res is None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return format_attendance_record(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        if status_filter not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularizes (manually corrects) one day's record, recomputing derived fields and appending an audit history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Supplying punch_in or punch_out with ABSENT or LEAVE is not allowed")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in_dt = None
        target_punch_out_dt = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in_dt, _ = epoch_ms_to_dt_truncated(body.punch_in)
        else:
            target_punch_in_dt = record.get("punch_in")

        if target_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        if target_punch_in_dt.tzinfo is None:
            target_punch_in_dt = target_punch_in_dt.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date (R1)
        calc_att_date = calculate_attendance_date(target_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_att_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in must stay on record's attendance date {date}")

        target_late_minutes = compute_late_minutes(target_punch_in_dt, emp["shift_start"], date)

        if body.punch_out is not None:
            target_punch_out_dt, _ = epoch_ms_to_dt_truncated(body.punch_out)
        else:
            if record.get("status") in ("ABSENT", "LEAVE"):
                target_punch_out_dt = None
            else:
                target_punch_out_dt = record.get("punch_out")

        if target_punch_out_dt is not None:
            if target_punch_out_dt.tzinfo is None:
                target_punch_out_dt = target_punch_out_dt.replace(tzinfo=timezone.utc)

            sec_diff = (target_punch_out_dt - target_punch_in_dt).total_seconds()
            if sec_diff <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if sec_diff > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            target_work_hours = compute_work_hours(target_punch_in_dt, target_punch_out_dt)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime_minutes = compute_overtime(target_punch_out_dt, emp["shift_start"], emp["shift_end"], date)
        else:
            target_work_hours = None
            target_half_day = False
            target_overtime_minutes = 0

    # Determine what actually changed
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes: Dict[str, Any] = {}

    if record.get("status") != target_status:
        changes["status"] = {"from": record.get("status"), "to": target_status}
    if old_punch_in != target_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in_dt}
    if old_punch_out != target_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out_dt}
    if record.get("work_hours") != target_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": target_work_hours}
    if record.get("late_minutes", 0) != target_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": target_late_minutes}
    if record.get("overtime_minutes", 0) != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": target_overtime_minutes}
    if record.get("half_day", False) != target_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": target_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = record.get("history", [])
    expected_history_len = len(current_history)

    # Concurrency safe update: ensure no history entry was added concurrently
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history_len]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in_dt,
                "punch_out": target_punch_out_dt,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if res is None:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon=2..Fri=6
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    month_start = f"{month}-01"
    last_day = calendar.monthrange(year, m)[1]
    month_end = f"{month}-{last_day:02d}"

    # Calculate working days per R7
    if emp["joined_on"] > month_end:
        working_days = 0
    else:
        eff_start = max(month_start, emp["joined_on"])
        working_days = count_working_days(eff_start, month_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    agg_result = list(db.attendance_logs.aggregate(pipeline))

    if agg_result:
        res = agg_result[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list[Dict[str, Any]]:
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "work_hours": 1,
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0.0]}},
                "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0.0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month via MongoDB aggregation cross-collection."""
    year, m = map(int, month.split("-"))
    month_start = f"{month}-01"
    last_day = calendar.monthrange(year, m)[1]
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        wh_count = d.get("work_hours_count", 0)
        wh_sum = d.get("work_hours_sum", 0.0)
        avg_work_hours = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        items.append({
            "department": d["_id"],
            "headcount": int(d["headcount"]),
            "present_days": float(d["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": int(d["late_count"]),
            "total_late_minutes": int(d["total_late_minutes"]),
            "leave_count": int(d["leave_count"]),
            "on_duty_count": int(d["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> list[Dict[str, Any]]:
    pipeline: list[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Ranks employees by total late minutes in the month using standard competition ranking ($rank)."""
    year, m = map(int, month.split("-"))
    month_start = f"{month}-01"
    last_day = calendar.monthrange(year, m)[1]
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[Dict[str, Any]]:
    from_dt = datetime.combine(dt_date.fromisoformat(from_date), time.min, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime.combine(dt_date.fromisoformat(to_date) + timedelta(days=1), time.min, tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$project": {
                "date": 1,
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_present",
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": ["$is_late", 1, 0]}
                },
            }
        },
        # Union with a single document to guarantee $densify has at least 1 document even if department has 0 logs
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_dt": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "date": "$date_dt",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": {"date": "$date_dt", "timezone": "UTC"}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap filling and a 7-day moving average."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    days_count = (d_to - d_from).days + 1
    if days_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Check department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        att_rate = d.get("attendance_rate")
        mov_avg = d.get("moving_avg_7d")

        items.append({
            "date": d["date"],
            "is_working_day": bool(d["is_working_day"]),
            "headcount": int(d["headcount"]),
            "present_count": float(d["present_count"]),
            "late_count": int(d["late_count"]),
            "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
            "moving_avg_7d": round_half_up(mov_avg, 4) if mov_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs MongoDB explain with executionStats verbosity for an endpoint's main query."""
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint: {endpoint}")

    try:
        if endpoint == "attendance_list":
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status_filter:
                q["status"] = status_filter

            cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
            explain_result = cursor.explain()
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            year, m = map(int, month.split("-"))
            month_start = f"{month}-01"
            last_day = calendar.monthrange(year, m)[1]
            month_end = f"{month}-{last_day:02d}"

            pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
            explain_cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
            explain_result = db.command("explain", explain_cmd, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, m = map(int, month.split("-"))
            month_start = f"{month}-01"
            last_day = calendar.monthrange(year, m)[1]
            month_end = f"{month}-{last_day:02d}"

            pipeline = build_department_summary_pipeline(month_start, month_end, department)
            explain_cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
            explain_result = db.command("explain", explain_cmd, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            year, m = map(int, month.split("-"))
            month_start = f"{month}-01"
            last_day = calendar.monthrange(year, m)[1]
            month_end = f"{month}-{last_day:02d}"

            pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit or 10, department)
            explain_cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
            explain_result = db.command("explain", explain_cmd, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="department, from, and to are required")

            pipeline = build_department_trend_pipeline(department, from_date, to_date)
            explain_cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
            explain_result = db.command("explain", explain_cmd, verbosity="executionStats")
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Explain failed: {e}")
