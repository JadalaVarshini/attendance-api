"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml.  Stored data: DATA_MODEL.md.  Review of the starter: REVIEW.md.
"""
import calendar
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

load_dotenv()  # override=False: variables already in the environment take priority

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MAX_RANGE_DAYS = 92


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same spec is a no-op."""
    specs = [
        # employees
        ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
        ("employees", [("joined_on", ASCENDING)], {"name": "joined_on"}),
        # attendance_logs
        # one record per (emp_code, date): the guarantee that makes punch-in race-safe
        ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_date_unique"}),
        # GET /attendance default order (date desc, emp_code asc) without an emp_code filter
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        # GET /attendance?status=...
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        # punch-out: most recent record of an employee with punch_in <= t
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
        # late leaderboard: date range + late_minutes > 0, grouped by emp_code (covered by the index)
        ("attendance_logs", [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], {"name": "date_late_emp"}),
    ]
    for coll, keys, opts in specs:
        try:
            db[coll].create_index(keys, **opts)
        except ServerSelectionTimeoutError as exc:  # Mongo unreachable: don't stall startup on every index
            log.error("MongoDB unreachable, skipping index creation: %s", exc)
            return
        except PyMongoError as exc:  # e.g. pre-existing duplicate data blocks a unique index
            log.error("index %s on %s failed: %s", opts.get("name"), coll, exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (instants are epoch ms in the API, UTC datetimes in MongoDB)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def parse_date(value: str, name: str = "date") -> date_cls:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    try:
        return date_cls(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")


def month_bounds(month: str) -> Tuple[date_cls, date_cls]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "month must be a valid YYYY-MM")
    return date_cls(year, mon, 1), date_cls(year, mon, calendar.monthrange(year, mon)[1])


def half_up(value: Any, places: int) -> float:
    """Round half-up (not banker's, not binary-float rounding). round(.., 9) strips float noise first."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(value), 9))).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def _hm(value: str) -> Tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def shift_of(emp: dict) -> Tuple[str, str]:
    return emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST calendar date of the punch-in; overnight shifts started the previous day if
    the punch-in's IST time-of-day is earlier than shift_end."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute, ist.second) < (*_hm(shift_end), 0):
        d -= timedelta(days=1)
    return d


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> Tuple[datetime, datetime]:
    """Shift start / end instants for the shift that belongs to attendance date `date_str`."""
    d = date_cls.fromisoformat(date_str)
    sh, sm = _hm(shift_start)
    eh, em = _hm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    seconds = int((punch_in - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals, in exact integer arithmetic."""
    seconds = int((punch_out - punch_in).total_seconds())
    hundredths = (seconds * 100 * 2 + 3600) // 7200  # floor(seconds*100/3600 + 1/2)
    return hundredths / 100


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive(status: str, p_in: Optional[datetime], p_out: Optional[datetime], date_str: str, emp: dict) -> dict:
    """All derived fields for a record, from its final state."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or p_in is None:
        return out
    s_start, s_end = shift_of(emp)
    start_dt, end_dt = shift_bounds(date_str, s_start, s_end)
    out["late_minutes"] = compute_late_minutes(p_in, start_dt)
    if p_out is not None:
        out["work_hours"] = compute_work_hours(p_in, p_out)
        out["overtime_minutes"] = compute_overtime(p_out, end_dt)
        out["half_day"] = out["work_hours"] < 4.5
    return out


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def to_record(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        history.append(
            {
                "at": dt_to_ms(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                    for k, c in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def to_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
# strict=True: floats, strings and booleans are rejected, not coerced.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls(int(v[:4]), int(v[5:7]), int(v[8:10]))  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceT = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # explicit null is rejected (type is not Optional); omitted fields keep the default None
    status: StatusT = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [to_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    s_start, s_end = shift_of(emp)
    date_str = attendance_date(ts, s_start, s_end).isoformat()
    start_dt, _ = shift_bounds(date_str, s_start, s_end)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return to_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = doc["punch_in"]
    if ts <= p_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - p_in > timedelta(hours=24):
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")
    d = derive(doc["status"], p_in, ts, doc["date"], emp)
    changes = {
        "punch_out": ts,
        "work_hours": d["work_hours"],
        "overtime_minutes": d["overtime_minutes"],
        "half_day": d["half_day"],
    }
    # the punch_out: None condition makes the transition open -> closed atomic (one 200 only)
    res = db.attendance_logs.update_one({"_id": doc["_id"], "punch_out": None}, {"$set": changes})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    doc.update(changes)
    return to_record(doc)


def _list_filter(emp_code, date_from, date_to, status) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date(date_from, "date_from") if date_from is not None else None
    d_to = parse_date(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


LIST_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = _list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(LIST_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [to_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


def _state(doc: dict) -> dict:
    """Comparable view of a stored record (missing derived fields mean 0 / false)."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def _apply_regularization(doc: dict, emp: dict, body: RegularizeIn) -> Tuple[dict, dict]:
    """Pure function: (new state, changes). Raises 422 on rule violations."""
    old = _state(doc)
    sent = body.model_fields_set
    new_status = body.status if "status" in sent else old["status"]
    sent_in = ms_to_dt(body.punch_in) if "punch_in" in sent else None
    sent_out = ms_to_dt(body.punch_out) if "punch_out" in sent else None

    if new_status in ("ABSENT", "LEAVE"):
        if sent_in is not None or sent_out is not None:
            raise HTTPException(422, f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None
    else:
        new_in = sent_in if sent_in is not None else old["punch_in"]
        new_out = sent_out if sent_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, f"status {new_status} requires a punch_in")
        s_start, s_end = shift_of(emp)
        if attendance_date(new_in, s_start, s_end).isoformat() != doc["date"]:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise HTTPException(422, "request changes nothing")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(new_status, new_in, new_out, doc["date"], emp))
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee_or_404(emp_code)
    for attempt in range(8):
        doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if doc is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = _apply_regularization(doc, emp, body)
        entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: only write if the record is exactly as we read it (history length
        # and the editable fields). A concurrent edit/punch-out makes this match nothing -> re-read.
        if "history" in doc:
            hist_cond: Any = {"$size": len(doc["history"] or [])}
        else:
            hist_cond = {"$exists": False}
        flt = {
            "_id": doc["_id"],
            "status": doc["status"],
            "punch_in": doc.get("punch_in"),
            "punch_out": doc.get("punch_out"),
            "history": hist_cond,
        }
        res = db.attendance_logs.update_one(flt, {"$set": new, "$push": {"history": entry}})
        if res.matched_count == 1:
            doc.update(new)
            doc["history"] = list(doc.get("history") or []) + [entry]
            return to_record(doc)
        time.sleep(random.uniform(0.005, 0.03))
    raise HTTPException(409, "record was modified concurrently, please retry")


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
_PRESENT = {"$in": ["$status", PRESENCE]}
_WEIGHT = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}  # half day = 0.5 of a present day
_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$gt": [_LATE_MIN, 0]}
_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}


def _sum_if(cond: Any, value: Any = 1) -> dict:
    return {"$sum": {"$cond": [cond, value, 0]}}


def _round_half_up_expr(expr: Any, places: int) -> dict:
    """MongoDB's $round is banker's rounding; this is floor(x * 10^p + 0.5) / 10^p (half-up)."""
    f = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, f]}, 0.5000000001]}}, f]}


def employee_monthly_pipeline(emp_code: str, month: str) -> List[dict]:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": _sum_if({"$and": [_PRESENT, _WEEKDAY]}, _WEIGHT),
                "leave_days": _sum_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": _sum_if(_IS_LATE),
                "total_late_minutes": {"$sum": _LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> List[dict]:
    first, last = month_bounds(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined by month end
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # one stats document per employee (employees with no logs get an empty array -> zeros)
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": _sum_if({"$and": [_PRESENT, _WEEKDAY]}, _WEIGHT),
                            "wh_sum": _sum_if({"$and": [_PRESENT, {"$isNumber": "$work_hours"}]}, "$work_hours"),
                            "wh_n": _sum_if({"$and": [_PRESENT, {"$isNumber": "$work_hours"}]}),
                            "late_count": _sum_if(_IS_LATE),
                            "total_late_minutes": {"$sum": _LATE_MIN},
                            "leave_count": _sum_if({"$eq": ["$status", "LEAVE"]}),
                            "on_duty_count": _sum_if({"$eq": ["$status", "ON_DUTY"]}),
                        }
                    },
                ],
                "as": "s",
            }
        },
        {"$addFields": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}}
                    for k in ("present_days", "wh_sum", "wh_n", "late_count", "total_late_minutes",
                              "leave_count", "on_duty_count")
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> List[dict]:
    first, last = month_bounds(month)
    pipe: List[dict] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipe.append({"$match": {"e.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipe


def department_employees(department: str) -> List[dict]:
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    if not emps:
        raise HTTPException(404, "department not found")
    return emps


def trend_range(frm: str, to: str) -> Tuple[date_cls, date_cls, int]:
    d_from, d_to = parse_date(frm, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    n = (d_to - d_from).days + 1
    if n > MAX_RANGE_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_RANGE_DAYS} days")
    return d_from, d_to, n


def department_trend_pipeline(department: str, frm: str, to: str) -> List[dict]:
    d_from, d_to, n = trend_range(frm, to)
    emps = department_employees(department)
    codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps if e.get("joined_on"))
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        # $facet always emits one document, even when there are no logs, so every day is still produced
        {
            "$facet": {
                "days": [
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": _sum_if(_PRESENT, _WEIGHT),
                            "late_count": _sum_if(_IS_LATE),
                        }
                    }
                ]
            }
        },
        # gap filling: one row per calendar day generated in the database ($range), no Python loop
        {
            "$project": {
                "days": 1,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {"dt": {"$add": [from_dt, {"$multiply": ["$$i", 86400000]}]}},
                                "in": {
                                    "date": {"$dateToString": {"date": "$$dt", "format": "%Y-%m-%d"}},
                                    "dow": {"$isoDayOfWeek": "$$dt"},
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {
            "$project": {
                "date": "$rows.date",
                "dow": "$rows.dow",
                "st": {
                    "$arrayElemAt": [
                        {"$filter": {"input": "$days", "as": "d", "cond": {"$eq": ["$$d._id", "$rows.date"]}}},
                        0,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": ["$dow", 5]},
                "headcount": {
                    "$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}
                },
                "present_count": {"$ifNull": ["$st.present_count", 0]},
                "late_count": {"$ifNull": ["$st.late_count", 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # mean of the non-null rates in the current row and up to 6 preceding rows (inside the range)
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [{"$eq": ["$ma", None]}, None, _round_half_up_expr("$ma", 4)]},
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
def working_days_in_month(month: str, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri days in the month, counted from joined_on for a mid-month joiner."""
    first, last = month_bounds(month)
    start = first
    if joined_on:
        start = max(first, date_cls.fromisoformat(joined_on))
    count, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {}
    working_days = working_days_in_month(month, emp.get("joined_on"))
    present = float(s.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": int(s.get("leave_days", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(present / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        wh_n = int(r["wh_n"])
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": half_up(r["present_days"], 2),
                # mean over records (sum / count), not a mean of per-employee means
                "avg_work_hours": half_up(Decimal(repr(round(float(r["wh_sum"]), 6))) / wh_n, 2) if wh_n else None,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    pipeline = department_trend_pipeline(department, from_, to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params: Any) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


def _explain(collection: str, command: Dict[str, Any]) -> dict:
    try:
        raw = db.command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(500, f"explain failed: {exc}")
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = _list_filter(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": dict(LIST_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            pipeline, collection = employee_monthly_pipeline(emp_code, month), "attendance_logs"
        elif endpoint == "department_summary":
            _require(month=month)
            pipeline, collection = department_summary_pipeline(month, department), "employees"
        elif endpoint == "late_leaderboard":
            _require(month=month)
            pipeline, collection = late_leaderboard_pipeline(month, limit, department), "attendance_logs"
        else:
            _require(department=department, **{"from": from_, "to": to})
            pipeline, collection = department_trend_pipeline(department, from_, to), "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(collection, command)}
