"""Employee Attendance & Analytics API."""
import os
import re
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def epoch_dt(ms: int) -> datetime:
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 100_000_000_000 or ms > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)

def dt_ms(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)

def public_doc(doc):
    if not doc: return doc
    d = dict(doc)
    d.pop("_id", None)
    if "created_at" in d: d["created_at"] = dt_ms(d["created_at"])
    for k in ("punch_in", "punch_out"):
        if k in d: d[k] = dt_ms(d[k])
    if "history" in d:
        hist=[]
        for entry in d.get("history") or []:
            e=dict(entry); e["at"]=dt_ms(e.get("at"))
            changes={}
            for key, change in (e.get("changes") or {}).items():
                c=dict(change)
                if key in ("punch_in","punch_out"):
                    c["from"]=dt_ms(c.get("from")); c["to"]=dt_ms(c.get("to"))
                changes[key]=c
            e["changes"]=changes; hist.append(e)
        d["history"]=hist
    return d

def hm(value):
    h,m=map(int,value.split(":")); return h*60+m

def shift_start_for_date(day: str, shift_start: str):
    h,m=map(int,shift_start.split(":"))
    y,mo,da=map(int,day.split("-"))
    return datetime(y,mo,da,h,m,tzinfo=IST).astimezone(UTC)

def shift_end_for_date(day: str, shift_start: str, shift_end: str):
    h,m=map(int,shift_end.split(":"))
    y,mo,da=map(int,day.split("-"))
    enddate=date(y,mo,da)
    if hm(shift_end) <= hm(shift_start): enddate += timedelta(days=1)
    return datetime.combine(enddate, datetime.min.time(), IST).replace(hour=h,minute=m).astimezone(UTC)

def attendance_day(ts: datetime, emp):
    local=ts.astimezone(IST)
    day=local.date()
    if hm(emp["shift_end"]) <= hm(emp["shift_start"]) and local.hour*60+local.minute < hm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()

def late_minutes(ts, day, start):
    delta=(ts-shift_start_for_date(day,start)).total_seconds()
    return int(delta//60) if delta > 600 else 0

def work_hours(pin,pout):
    val=Decimal(str((pout-pin).total_seconds()))/Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def overtime_minutes(pout, day, start, end):
    delta=(pout-shift_end_for_date(day,start,end)).total_seconds()
    minutes=int(delta//60)
    return minutes if minutes >= 30 else 0

def round_half(v, places=2):
    if v is None: return None
    return float(Decimal(str(v)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))

def validate_date(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise HTTPException(422, "invalid date; expected YYYY-MM-DD")
    try: return date.fromisoformat(s)
    except Exception: raise HTTPException(422, "invalid date; expected YYYY-MM-DD")

def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,month.split("-"))
    start=date(y,m,1); end=date(y+1,1,1) if m==12 else date(y,m+1,1)
    return start.isoformat(),end.isoformat()

def weekdays(start, end):
    a=validate_date(start); b=validate_date(end)
    n=0
    while a < b:
        if a.weekday()<5: n+=1
        a+=timedelta(days=1)
    return n

@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code",1)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department",1),("joined_on",1)], name="ix_department_joined")
    db.attendance_logs.create_index([("emp_code",1),("date",1)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date",-1),("emp_code",1)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code",1),("date",-1),("status",1)], name="ix_attendance_emp_date_status")
    db.attendance_logs.create_index([("date",1),("status",1),("late_minutes",1)], name="ix_attendance_date_status_late")

@app.get("/health")
def health():
    try: db.command("ping")
    except Exception: raise HTTPException(503, "MongoDB unavailable")
    return {"status":"ok"}

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1,max_length=50)
    shift_start: str = Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_valid(cls,v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v): raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("punched_at must be epoch milliseconds")
        return v

class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt]=None
    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("punched_at must be epoch milliseconds")
        return v

class RegularizeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None
    punch_in: Optional[StrictInt]=None
    punch_out: Optional[StrictInt]=None
    reason: str=Field(min_length=5,max_length=200)
    regularized_by: str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def ts_valid(cls,v):
        if v is not None and (isinstance(v,bool) or not isinstance(v,int) or not 100_000_000_000 <= v <= 4_102_444_800_000): raise ValueError("timestamp must be epoch milliseconds")
        return v
    @model_validator(mode="after")
    def no_null_punch_values(self):
        for key in ("punch_in", "punch_out"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} must be epoch milliseconds when supplied")
        return self

@app.post("/employees",status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return public_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if department is not None: q["department"]=department
    total=db.employees.count_documents(q)
    items=[public_doc(x) for x in db.employees.find(q,{"_id":0}).sort("emp_code",1).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=epoch_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day=attendance_day(ts,emp)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":late_minutes(ts,day,emp["shift_start"]),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return public_doc(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=epoch_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",-1)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"record already punched out")
    if ts <= rec["punch_in"] or (ts-rec["punch_in"]).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    day=rec["date"]; wh=work_hours(rec["punch_in"],ts); ot=overtime_minutes(ts,day,emp["shift_start"],emp["shift_end"])
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},
       {"$set":{"punch_out":ts,"work_hours":wh,"overtime_minutes":ot,"half_day":wh<4.5}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record already punched out")
    return public_doc(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,
                    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: validate_date(date_from)
    if date_to: validate_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must not exceed date_to")
    q={}
    if emp_code is not None:q["emp_code"]=emp_code
    if status is not None:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    items=[public_doc(x) for x in db.attendance_logs.find(q,{"_id":0}).sort([("date",-1),("emp_code",1)]).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    day=date
    validate_date(day)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":day})
    if not rec: raise HTTPException(404,"attendance record not found")
    status=body.status if body.status is not None else rec["status"]
    pin=epoch_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    pout=epoch_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if status in ("ABSENT","LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422,"ABSENT/LEAVE cannot include punch times")
        pin=pout=None
    elif pin is None: raise HTTPException(422,"presence status requires punch_in")
    if pin is not None and attendance_day(pin,emp)!=day: raise HTTPException(422,"punch_in must belong to attendance date")
    if pout is not None:
        if pin is None or pout<=pin or (pout-pin).total_seconds()>86400: raise HTTPException(422,"invalid punch_out")
    proposed={"status":status,"punch_in":pin,"punch_out":pout}
    if status in ("ABSENT","LEAVE"):
        proposed.update(work_hours=None,late_minutes=0,overtime_minutes=0,half_day=False)
    else:
        proposed["late_minutes"]=late_minutes(pin,day,emp["shift_start"])
        if pout is None: proposed.update(work_hours=None,overtime_minutes=0,half_day=False)
        else:
            wh=work_hours(pin,pout); proposed.update(work_hours=wh,overtime_minutes=overtime_minutes(pout,day,emp["shift_start"],emp["shift_end"]),half_day=wh<4.5)
    tracked=("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")
    changes={}
    for k in tracked:
        old=rec.get(k)
        new=proposed.get(k)
        if old!=new:
            changes[k]={"from":old,"to":new}
    if not changes: raise HTTPException(422,"request does not change the record")
    hist={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    sets=dict(proposed)
    result=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"history":rec.get("history",[])},
       {"$set":sets,"$push":{"history":hist}},return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409,"record changed concurrently; retry")
    return public_doc(result)

PRESENT_EXPR={"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}
WEEKDAY_PRESENT_EXPR={"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},
    {"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},
    {"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},
    {"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    start,end=month_bounds(month)
    effective=max(start,emp["joined_on"])
    working=weekdays(effective,end) if effective<end else 0
    pipeline=[
      {"$match":{"emp_code":emp_code,"date":{"$gte":max(start,emp["joined_on"]),"$lt":end}}},
      {"$group":{"_id":None,"present_days":{"$sum":WEEKDAY_PRESENT_EXPR},
       "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
       "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
       "total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}
    ]
    r=list(db.attendance_logs.aggregate(pipeline))
    v=r[0] if r else {}
    pct=round_half(v.get("present_days",0)/working*100,2) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":round_half(v.get("present_days",0)),
      "leave_days":v.get("leave_days",0),"late_count":v.get("late_count",0),"total_late_minutes":v.get("total_late_minutes",0),
      "total_overtime_minutes":v.get("total_overtime_minutes",0),"attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department:Optional[str]=None):
    start,end=month_bounds(month)
    empmatch={"joined_on":{"$lt":end}}
    if department is not None: empmatch["department"]=department
    pipeline=[
      {"$match":empmatch},
      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code","joined":"$joined_on"},"pipeline":[
        {"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start]},{"$lt":["$date",end]},{"$gte":["$date","$$joined"]}]}}},
        {"$group":{"_id":None,"present_days":{"$sum":WEEKDAY_PRESENT_EXPR},
          "work_hours_sum":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$work_hours",None]}]},"$work_hours",0]}},
          "work_hours_count":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$work_hours",None]}]},1,0]}},
          "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
          "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
          "leave_count":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
          "on_duty_count":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}}}
      ],"as":"stats"}},
      {"$unwind":{"path":"$stats","preserveNullAndEmptyArrays":True}},
      {"$group":{"_id":"$department","headcount":{"$sum":1},"present_days":{"$sum":{"$ifNull":["$stats.present_days",0]}},
        "work_hours_sum":{"$sum":{"$ifNull":["$stats.work_hours_sum",0]}},"work_hours_count":{"$sum":{"$ifNull":["$stats.work_hours_count",0]}},"late_count":{"$sum":{"$ifNull":["$stats.late_count",0]}},
        "total_late_minutes":{"$sum":{"$ifNull":["$stats.total_late_minutes",0]}},
        "leave_count":{"$sum":{"$ifNull":["$stats.leave_count",0]}},"on_duty_count":{"$sum":{"$ifNull":["$stats.on_duty_count",0]}}}},
      {"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":1,"avg_work_hours":{"$cond":[{"$gt":["$work_hours_count",0]},{"$divide":["$work_hours_sum","$work_hours_count"]},None]},
        "late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
      {"$sort":{"department":1}}
    ]
    items=list(db.employees.aggregate(pipeline))
    for x in items:
        x["present_days"]=round_half(x["present_days"])
        x["avg_work_hours"]=round_half(x["avg_work_hours"])
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=month_bounds(month)
    pipeline=[
      {"$match":{"date":{"$gte":start,"$lt":end},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},
      {"$unwind":"$emp"},
    ]
    if department is not None:pipeline.append({"$match":{"emp.department":department}})
    pipeline += [
      {"$set":{"emp_code":"$_id","name":"$emp.name","department":"$emp.department"}},
      {"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},
      {"$sort":{"total_late_minutes":-1,"emp_code":1}},
      {"$project":{"_id":0,"rank":1,"emp_code":1,"name":1,"department":1,"total_late_minutes":1,"late_count":1}}
    ]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_date:str=Query(...,alias="from"),to_date:str=Query(...,alias="to")):
    a=validate_date(from_date); b=validate_date(to_date)
    if b<a or (b-a).days+1>92: raise HTTPException(422,"invalid date range; maximum 92 days")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    start_dt=datetime.combine(a,datetime.min.time(),UTC); end_dt=datetime.combine(b+timedelta(days=1),datetime.min.time(),UTC)
    pipeline=[
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
      {"$unwind":"$emp"},{"$match":{"emp.department":department,"date":{"$gte":from_date,"$lte":to_date}}},
      {"$set":{"day_dt":{"$dateFromString":{"dateString":"$date"}}}},
      {"$group":{"_id":"$day_dt","present_count":{"$sum":PRESENT_EXPR},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
      {"$project":{"_id":0,"day_dt":"$_id","present_count":1,"late_count":1}},
      {"$densify":{"field":"day_dt","range":{"step":1,"unit":"day","bounds":[start_dt,end_dt]}}},
      {"$set":{"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]},
        "is_working_day":{"$and":[{"$gte":[{"$dayOfWeek":"$day_dt"},2]},{"$lte":[{"$dayOfWeek":"$day_dt"},6]}]}}},
      {"$lookup":{"from":"employees","let":{"day":{"$dateToString":{"date":"$day_dt","format":"%Y-%m-%d"}}},"pipeline":[
        {"$match":{"department":department,"$expr":{"$lte":["$joined_on","$$day"]}}},{"$count":"n"}],"as":"hc"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]} }},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":["$present_count","$headcount"]},None]}}},
      {"$setWindowFields":{"sortBy":{"day_dt":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":{"$dateToString":{"date":"$day_dt","format":"%Y-%m-%d"}},"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":1}},
      {"$sort":{"date":1}}
    ]
    items=list(db.attendance_logs.aggregate(pipeline))
    for x in items:
        x["present_count"]=round_half(x.get("present_count",0))
        x["attendance_rate"]=round_half(x.get("attendance_rate"),4)
        x["moving_avg_7d"]=round_half(x.get("moving_avg_7d"),4)
        x["is_working_day"]=bool(x.get("is_working_day"))
    return {"department":department,"items":items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
                     status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
                     month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),
                     from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to")):
    try:
        if endpoint=="attendance_list":
            q={}
            if emp_code is not None:q["emp_code"]=emp_code
            if status is not None:q["status"]=status
            if date_from or date_to:
                q["date"]={}
                if date_from:q["date"]["$gte"]=date_from
                if date_to:q["date"]["$lte"]=date_to
            return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command({"explain":{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size},"verbosity":"executionStats"})}
        if endpoint=="employee_monthly":
            if not emp_code or not month: raise HTTPException(422,"emp_code and month are required")
            start,end=month_bounds(month)
            pipe=[{"$match":{"emp_code":emp_code,"date":{"$gte":start,"$lt":end}}},{"$group":{"_id":"$emp_code","late":{"$sum":"$late_minutes"}}}]
            return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})}
        if endpoint=="department_summary":
            if not month:raise HTTPException(422,"month is required")
            start,end=month_bounds(month)
            pipe=[{"$match":{"joined_on":{"$lt":end}, **({"department":department} if department is not None else {})}}]
            return {"endpoint":endpoint,"collection":"employees","explain":db.command({"explain":{"aggregate":"employees","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})}
        if endpoint=="late_leaderboard":
            if not month:raise HTTPException(422,"month is required")
            start,end=month_bounds(month)
            pipe=[{"$match":{"date":{"$gte":start,"$lt":end},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total":{"$sum":"$late_minutes"}}}]
            return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})}
        if endpoint=="department_trend":
            if not department or not from_date or not to_date:raise HTTPException(422,"department, from and to are required")
            pipe=[{"$match":{"date":{"$gte":from_date,"$lte":to_date}}},{"$group":{"_id":"$date","count":{"$sum":1}}}]
            return {"endpoint":endpoint,"collection":"attendance_logs","explain":db.command({"explain":{"aggregate":"attendance_logs","pipeline":pipe,"cursor":{}},"verbosity":"executionStats"})}
        raise HTTPException(422,"unknown endpoint")
    except HTTPException: raise
    except Exception as exc: raise HTTPException(422,f"cannot explain endpoint: {exc}")
