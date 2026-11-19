# -*- coding: utf-8 -*-
"""
Poli-e Man API v11 — نسخه نهایی ابری (سازگار با Render)
"""
import asyncio, base64, json, logging, os, re, ssl, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict

import httpx, uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── تنظیمات SSL (برای ارتباط امن با سرور پلیس‌من) ───
try:
    import truststore
    VERIFY = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    VERIFY = True

# ─── مسیرها ───
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

# ─── تنظیمات محیطی (از پنل Render خوانده می‌شود) ───
# توکن را در Render به عنوان Environment Variable با نام PM_API_TOKEN تنظیم کنید
API_TOKEN = os.getenv("PM_API_TOKEN", "pm_GddJ_P6EqQgdUtqw7FOILpOuxYSjTxhgcOWrFEXf_wc")
POLICE_BASE = "https://police-man.ir"
POLICE_FRONT = "https://pwa.police-man.ir"

# ─── لاگ‌ها ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("srv")

# ─── هدرهای پلیس‌من ───
POLICE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "fa-IR,fa;q=0.9",
    "Content-Type": "application/json",
    "Origin": POLICE_FRONT, "Referer": POLICE_FRONT + "/", "pwa": "true",
}

# ═══════════════════════════════════════════════════════════
#  ابزارهای کمکی
# ═══════════════════════════════════════════════════════════
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA2EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def to_fa(v):
    return "" if v is None else str(v).translate(_FA)

def only_digits(v):
    if v is None: return ""
    return re.sub(r"\D", "", str(v).translate(_FA2EN))

def amount_int(v):
    try: return int(float(str(v).replace(",", "").strip()))
    except: return None

def money_fa(a):
    if a is None: return None
    return f"{a:,}".replace(",", "٬").translate(_FA) + " ریال"

# ─── پلاک ───
PLATE_LETTERS = {"01":"الف","02":"ب","03":"ت","04":"ج","05":"د","06":"س","07":"ص","08":"ط","09":"ع","10":"ق","11":"ل","12":"م","13":"ن","14":"و","15":"ه","16":"ی","17":"ر","18":"ک","19":"♿","20":"گ","21":"D","23":"ز","24":"ظ","25":"ض","26":"ذ"}

def parse_plate(plate):
    s = only_digits(plate)
    if not s: return {"valid":False,"type":None,"raw":str(plate or ""),"text":str(plate or ""),"text_fa":""}
    if len(s) == 9:
        letter = PLATE_LETTERS.get(s[2:4], "?")
        text = f"{s[4:6]} {letter} {s[6:9]} - {s[0:2]}"
        return {"valid":True,"type":"car","raw":s,"text":text,"text_fa":text.translate(_FA)}
    text = f"{s[2:5]} / {s[5:10]}" if len(s) >= 10 else s
    return {"valid":True,"type":"motor","raw":s,"text":text,"text_fa":text.translate(_FA)}

def enrich(v):
    info = parse_plate(v.get("plate"))
    amt = amount_int(v.get("violation"))
    return {**v, "plate_info":info, "plate_text_fa":info.get("text_fa"), "amount":amt, "amount_fa":money_fa(amt)}

def extract_image(node, depth=0):
    if depth > 6 or node is None: return None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and len(v) > 300 and any(h in k.lower() for h in ("profileimage","avatar","photo","image")):
                return v
        for v in node.values():
            r = extract_image(v, depth+1)
            if r: return r
    elif isinstance(node, list):
        for it in node[:20]:
            r = extract_image(it, depth+1)
            if r: return r
    return None

def image_url(b64):
    if not b64: return None
    try: raw = base64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4))
    except: raw = b""
    mime = "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/png"
    return f"data:{mime};base64,{b64}"

def personal_view(p, img):
    if not p: return None
    first = p.get("firstName") or ""; last = p.get("lastName") or ""
    name = (first + " " + last).strip() or None
    return {"firstName":first or None,"lastName":last or None,"fullName":name,"nationalId":p.get("nationalId"),"nationalId_fa":to_fa(p.get("nationalId")),"birthDate":p.get("birthDate"),"birthDate_fa":to_fa(p.get("birthDate")),"mobileNo":p.get("mobileNo"),"mobileNo_fa":to_fa(p.get("mobileNo")),"address":p.get("address"),"postalCode":p.get("postalCode"),"hasProfileImage":bool(img),"profileImageDataUrl":image_url(img)}

# ─── مدیریت سشن‌ها ───
def session_path(cid): return SESSIONS_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', cid)[:80]}.json"

def load_session(cid):
    p = session_path(cid)
    if not p.exists(): return {}
    try: return json.loads(p.read_text(encoding="utf-8"))
    except: return {}

def save_session(cid, data):
    session_path(cid).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ─── کلاینت HTTP ───
_clients: Dict[str, httpx.AsyncClient] = {}

def get_client(cid):
    c = _clients.get(cid)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(base_url=POLICE_BASE, timeout=httpx.Timeout(30.0, connect=15.0), verify=VERIFY, headers=POLICE_HEADERS, trust_env=False, follow_redirects=True)
        _clients[cid] = c
    return c

async def close_all():
    for c in list(_clients.values()):
        try:
            if not c.is_closed: await c.aclose()
        except: pass
    _clients.clear()

@asynccontextmanager
async def lifespan(app):
    log.info("=" * 60)
    log.info("Poli-e Man API v11.0.0 (Cloud Mode)")
    log.info("=" * 60)
    yield
    await close_all()

app = FastAPI(title="Poli-e Man v11", version="11.0.0", lifespan=lifespan)

# ─── CORS کاملاً باز ───
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"], expose_headers=["*"], max_age=3600)

# ─── بررسی‌های امنیتی ───
def check_token(t):
    if not t or t != API_TOKEN: raise HTTPException(401, "توکن API نامعتبر است")

def check_cid(c):
    c = (c or "").strip()
    if not c or len(c) < 8 or len(c) > 80: raise HTTPException(400, "X-Client-Id نامعتبر")
    return c

class LoginReq(BaseModel):
    phone: str
    national_id: str

class VerifyReq(BaseModel):
    phone: str
    national_id: str
    code: str

# ═══════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {"name":"Poli-e Man API","status":"online","version":"11.0.0"}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "11.0.0", "time": int(time.time())}

@app.get("/status")
async def status(x_api_token: Optional[str] = Header(None, alias="X-API-Token"), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    check_token(x_api_token)
    cid = check_cid(x_client_id)
    sess = load_session(cid)
    return {"server":"online","version":"11.0.0","police_session":bool(sess.get("access_token")),"personal":personal_view(sess.get("personal"), sess.get("profile_image"))}

@app.post("/api/all")
async def api_all(x_api_token: Optional[str] = Header(None, alias="X-API-Token"), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    check_token(x_api_token)
    cid = check_cid(x_client_id)
    sess = load_session(cid)
    token = sess.get("access_token")
    personal = sess.get("personal") or {}
    img = sess.get("profile_image")
    birthdate = sess.get("birthdate") or personal.get("birthDate") or "1356/01/01"
    
    if not token:
        return {"personal":None,"violations":{"items":[],"count":0},"message":"ابتدا وارد شوید"}
        
    try:
        r = await get_client(cid).post("/api/violations", json={"parameters":{"birthdate":birthdate},"token":token}, cookies=sess.get("cookies") or {})
        data = r.json()
        raw = data.get("response") or []
        items = [enrich(v) for v in raw] if isinstance(raw, list) else []
        total = sum(i["amount"] for i in items if isinstance(i.get("amount"), int))
        return {"personal":personal_view(personal,img),"violations":{"items":items,"count":len(items),"total_amount":total,"total_amount_fa":money_fa(total)}}
    except httpx.HTTPError as e:
        return {"personal":personal_view(personal,img),"violations":{"items":[],"error":f"ارتباط: {e}"}}
    except Exception as e:
        return {"personal":personal_view(personal,img),"violations":{"items":[],"error":str(e)}}

@app.post("/auth/send-otp")
async def send_otp(req: LoginReq, x_api_token: Optional[str] = Header(None, alias="X-API-Token"), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    check_token(x_api_token)
    cid = check_cid(x_client_id)
    phone = only_digits(req.phone) or req.phone
    nid = only_digits(req.national_id) or req.national_id
    
    if not phone or not nid: raise HTTPException(400, "شماره و کد ملی الزامی است")
    
    try:
        cli = get_client(cid)
        r = await cli.post("/api/sendSms", json={"parameters":{"phoneNumber":phone,"nationalCode":nid}})
        sess = load_session(cid)
        sess["cookies"] = {c.name: c.value for c in cli.cookies.jar}
        sess["_pending_phone"] = phone
        sess["_pending_nid"] = nid
        save_session(cid, sess)
        return r.json()
    except httpx.HTTPError as e: raise HTTPException(502, f"ارتباط با پلیس‌من: {e}")
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/auth/verify")
async def verify_otp(req: VerifyReq, x_api_token: Optional[str] = Header(None, alias="X-API-Token"), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    check_token(x_api_token)
    cid = check_cid(x_client_id)
    phone = only_digits(req.phone) or req.phone
    nid = only_digits(req.national_id) or req.national_id
    code = only_digits(req.code) or req.code
    
    if not phone or not nid or not code: raise HTTPException(400, "همه فیلدها الزامی است")
    
    try:
        cli = get_client(cid)
        sess = load_session(cid)
        r = await cli.post("/api/verifySms", json={"parameters":{"phoneNumber":phone,"nationalCode":nid,"verifyCode":code}}, cookies=sess.get("cookies") or {})
        data = r.json()
        if data.get("resultCode") != 1: raise HTTPException(400, data.get("resultMessage", "کد نامعتبر"))
        
        resp = data.get("response") or {}
        personal = resp.get("personalDto") or {}
        save_session(cid, {"access_token":resp.get("token"),"birthdate":personal.get("birthDate") or "1356/01/01","personal":personal,"profile_image":resp.get("profileImage") or extract_image(resp),"cookies":{c.name:c.value for c in cli.cookies.jar}})
        return {"message":"ورود موفق","personal":personal_view(personal, resp.get("profileImage"))}
    except HTTPException: raise
    except httpx.HTTPError as e: raise HTTPException(502, f"ارتباط با پلیس‌من: {e}")
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/auth/logout")
async def logout(x_api_token: Optional[str] = Header(None, alias="X-API-Token"), x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    check_token(x_api_token)
    cid = check_cid(x_client_id)
    p = session_path(cid)
    if p.exists(): p.unlink()
    return {"message":"خارج شدید"}

# ─── اجرای سرور ───
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
