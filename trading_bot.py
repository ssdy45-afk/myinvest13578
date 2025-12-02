import os
import time
import json
from datetime import datetime, timedelta
import pytz
import requests
import pandas as pd
from dotenv import load_dotenv

# ========= 설정/환경 변수 =========
load_dotenv()

BASE = "https://openapi.koreainvestment.com:9443"

MODE = os.getenv("MODE", "paper").strip().lower()  # paper | live
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT = os.getenv("KIS_ACCOUNT")  # 앞 8자리
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_EXPIRES_AT = os.getenv("ACCESS_TOKEN_EXPIRES_AT")  # "YYYY-MM-DD HH:MM:SS" (KST)

# 전략/리스크
SYMBOL = os.getenv("SYMBOL", "AMD")
EXCH = os.getenv("EXCH", "NASD")
NMIN = os.getenv("NMIN", "1")

TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.008"))   # +0.8%
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.005"))       # -0.5%
DAILY_LIMIT = float(os.getenv("RISK_DAILY_LIMIT", "-0.03"))  # -3%

# 주문 안전장치
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "2000"))  # 1회 최대 주문 금액 상한
MAX_QTY_PER_ORDER = int(os.getenv("MAX_QTY_PER_ORDER", "50"))  # 1회 최대 수량 상한

KST = pytz.timezone("Asia/Seoul")


# ========= 유틸 =========
def now_kst():
    return datetime.now(KST)

def log(msg, data=None):
    ts = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if data is not None:
        try:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            print(data)
    # 파일 로그
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if data is not None:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass

def token_expiring_soon(exp_str, buffer_min=30):
    if not exp_str:
        return False
    try:
        exp_dt = KST.localize(datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        return now_kst() + timedelta(minutes=buffer_min) >= exp_dt
    except Exception:
        return False

def headers(tr_id):
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }

def is_us_market_open_kst(dt=None):
    """
    KST 기준 근사 장시간: 대략 22:30~06:00
    (DST 반영 필요시 캘린더 사용 권장)
    """
    dt = dt or now_kst()
    h = dt.hour
    # 간단 필터: 22~6시
    return (h >= 22) or (h <= 6)


# ========= 데이터 조회 =========
def get_intraday_bars():
    url = f"{BASE}/uapi/overseas-price/v1/quotations/inquire-time-series"
    h = headers(tr_id="HHDFS00000300")  # 해외주식 시세 조회 TR (모의/실 동일 조회용)
    params = {
        "EXCD": EXCH,
        "SYMB": SYMBOL,
        "NMIN": NMIN,
        "FID_ETC_CLS_CODE": "0",
        "FID_COND_MRKT_DIV_CODE": "J"
    }
    r = requests.get(url, headers=h, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    output = data.get("output2") or []
    df = pd.DataFrame(output)

    # 응답 컬럼 매핑 (환경에 따라 달라질 수 있음)
    close = None
    for cand in ("last", "stck_prpr", "close", "종가"):
        if cand in df.columns:
            close = pd.to_numeric(df[cand], errors="coerce")
            break
    if close is None:
        obj_cols = df.select_dtypes(include="object").columns
        if len(obj_cols) == 0:
            raise ValueError("응답에서 종가 컬럼을 찾지 못했습니다. 응답 구조 확인 필요.")
        close = pd.to_numeric(df[obj_cols[0]], errors="coerce")
    df["close"] = close
    return df


# ========= RSI =========
def compute_rsi(series: pd.Series, period=14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean().replace(0, 1e-9)
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def rsi_signal(rsi_last: float):
    if rsi_last < 25:
        return "BUY"
    elif rsi_last > 75:
        return "SELL"
    return "HOLD"


# ========= 포지션/리스크 =========
class Position:
    def __init__(self):
        self.side = None   # "LONG"/"SHORT"/None
        self.entry = None
        self.qty = 0

    def flat(self):
        return self.side is None or self.qty == 0

    def open(self, side, price, qty):
        self.side = side
        self.entry = price
        self.qty = qty

    def close(self):
        self.side = None
        self.entry = None
        self.qty = 0

def position_size(cash_usd: float, price: float, risk_pct=0.01, stop_pct=STOP_LOSS) -> int:
    """
    - 리스크 기반 수량: 계좌의 1% 리스크 기준
    - 안전장치: 1회 주문 상한 금액/수량 적용
    """
    if price <= 0:
        return 0
    # 주문 금액 상한
    qty_cap_by_usd = int(MAX_ORDER_USD / price)
    # 리스크 기반
    risk_amt = max(cash_usd * risk_pct, 1.0)
    per_share_risk = max(price * stop_pct, 0.01)
    qty_risk = int(risk_amt / per_share_risk)
    qty = max(min(qty_risk, qty_cap_by_usd, MAX_QTY_PER_ORDER), 0)
    return qty

def unrealized_pnl_pct(pos: Position, current_price: float):
    if pos.flat() or pos.entry is None or current_price <= 0:
        return 0.0
    if pos.side == "LONG":
        return (current_price - pos.entry) / pos.entry
    else:  # SHORT
        return (pos.entry - current_price) / pos.entry

def hit_take_profit(pos: Position, price: float):
    return unrealized_pnl_pct(pos, price) >= TAKE_PROFIT

def hit_stop_loss(pos: Position, price: float):
    return unrealized_pnl_pct(pos, price) <= -STOP_LOSS


# ========= 잔고/대시보드 =========
def get_cash_balance():
    """
    해외주식 잔고/현금 조회 (응답 구조는 환경에 따라 다를 수 있음)
    """
    url = f"{BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    h = headers(tr_id="HHDFS00000500")  # 잔고조회 TR (모의/실 공통 조회용)
    params = {
        "CANO": ACCOUNT,
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": EXCH,
        "ITEM_CD": ""  # 전체
    }
    r = requests.get(url, headers=h, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    cash = 0.0
    # 예시 필드: output1에 총 평가/현금 등, output에 종목 리스트
    try:
        if "output1" in data and len(data["output1"]) > 0:
            # 외화 현금잔고 필드가 환경마다 다를 수 있음. 예: "frcr_cblc_amt" 등
            candidates = ["frcr_cblc_amt", "frcr_evlu_amt", "frcr_buy_am"]
            for c in candidates:
                if c in data["output1"][0]:
                    cash = float(str(data["output1"][0].get(c, "0")).replace(",", ""))
                    break
    except Exception:
        pass
    return cash

def get_portfolio():
    url = f"{BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    h = headers(tr_id="HHDFS00000500")
    params = {
        "CANO": ACCOUNT,
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": EXCH,
        "ITEM_CD": ""
    }
    r = requests.get(url, headers=h, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    portfolio = []
    if "output" in data:
        for item in data["output"]:
            try:
                portfolio.append({
                    "종목코드": item.get("ovrs_pdno"),
                    "종목명": item.get("ovrs_item_name"),
                    "보유수량": int(float(str(item.get("ord_psbl_qty", "0")).replace(",", ""))) if item.get("ord_psbl_qty") else 0,
                    "평가금액": float(str(item.get("frcr_evlu_amt", "0")).replace(",", "")),
                    "평가손익": float(str(item.get("evlu_pfls_amt", "0")).replace(",", "")),
                    "수익률": float(str(item.get("evlu_pfls_rt", "0")).replace(",", "")),
                })
            except Exception:
                continue
    return portfolio

def get_total_eval():
    url = f"{BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    h = headers(tr_id="HHDFS00000500")
    params = {
        "CANO": ACCOUNT,
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": EXCH,
        "ITEM_CD": ""
    }
    r = requests.get(url, headers=h, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    total_eval = 0.0
    try:
        if "output1" in data and len(data["output1"]) > 0:
            # 예시: 총 외화 평가금액
            total_eval = float(str(data["output1"][0].get("frcr_evlu_amt", "0")).replace(",", ""))
    except Exception:
        pass
    return total_eval


# ========= 주문 =========
def tr_id_order(side: str):
    """
    모드별 TR ID 스위치:
    - 모의투자: VTT...
    - 실투자:  TTT...
    """
    s = side.upper()
    if MODE == "live":
        return "TTTS03010100" if s == "BUY" else "TTTS03010200"
    else:
        return "VTTS03010100" if s == "BUY" else "VTTS03010200"

def place_order(side, qty, symbol=SYMBOL, exch=EXCH, market=True, price=None):
    if qty <= 0:
        return {"error": "qty<=0", "message": "수량이 0 이하라 주문하지 않습니다."}

    url = f"{BASE}/uapi/overseas-stock/v1/trading/order"
    h = headers(tr_id=tr_id_order(side))
    payload = {
        "CANO": ACCOUNT,           # 앞 8자리
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": exch,
        "ITEM_CD": symbol,
        "OVRS_ORD_QTY": str(qty),
        "OVRS_ORD_DVSN": "02" if market else "01",  # 02: 시장가, 01: 지정가
        "OVRS_ORD_UNPR": "" if market else str(price or 0)
    }
    r = requests.post(url, headers=h, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ========= 메인 루프 =========
def run_bot():
    # 환경체크
    log("봇 시작", {
        "mode": MODE,
        "account": ACCOUNT,
        "symbol": SYMBOL,
        "exchange": EXCH,
        "tp": TAKE_PROFIT,
        "sl": STOP_LOSS,
        "daily_limit": DAILY_LIMIT,
        "max_order_usd": MAX_ORDER_USD,
        "max_qty_per_order": MAX_QTY_PER_ORDER
    })
    if MODE == "live":
        log("실투자 모드 경고: 실제 주문이 전송됩니다. 안전장치 확인하세요.")

    pos = Position()
    day_start = now_kst().date()
    day_realized_pnl_pct = 0.0

    while True:
        try:
            # 토큰 만료 경고
            if token_expiring_soon(ACCESS_TOKEN_EXPIRES_AT, buffer_min=30):
                log("토큰 만료 임박: ACCESS_TOKEN/만료시간 갱신 필요")

            # 날짜 변경 시 일손익 리셋
            if now_kst().date() != day_start:
                day_start = now_kst().date()
                day_realized_pnl_pct = 0.0
                log("새 거래일 시작: 일손익 리셋")

            # 장 시간 체크
            if not is_us_market_open_kst():
                log("미국장 비시간: 대기")
                time.sleep(60)
                continue

            # 대시보드
            cash_usd = get_cash_balance()
            portfolio = get_portfolio()
            total_eval = get_total_eval()
            log("계좌 대시보드", {"현금잔고": cash_usd, "총 평가금액": total_eval, "보유종목": portfolio})

            # 시세/RSI
            df = get_intraday_bars()
            close = pd.to_numeric(df["close"], errors="coerce").dropna()
            if len(close) < 20:
                log("캔들 부족: 대기")
                time.sleep(30)
                continue

            price = float(close.iloc[-1])
            rsi = float(compute_rsi(close, 14).iloc[-1])
            sig = rsi_signal(rsi)
            log("시그널", {"price": price, "rsi": round(rsi, 2), "signal": sig})

            # 포지션 관리: 익절/손절
            if not pos.flat():
                if hit_take_profit(pos, price):
                    log("익절 청산", {"entry": pos.entry, "price": price})
                    side = "SELL" if pos.side == "LONG" else "BUY"
                    resp = place_order(side, pos.qty)
                    log("청산 응답", resp)
                    day_realized_pnl_pct += unrealized_pnl_pct(pos, price)
                    pos.close()
                elif hit_stop_loss(pos, price):
                    log("손절 청산", {"entry": pos.entry, "price": price})
                    side = "SELL" if pos.side == "LONG" else "BUY"
                    resp = place_order(side, pos.qty)
                    log("청산 응답", resp)
                    day_realized_pnl_pct += unrealized_pnl_pct(pos, price)
                    pos.close()

            # 일일 손실 한도 체크
            if day_realized_pnl_pct <= DAILY_LIMIT:
                log("일일 손실 한도 도달: 거래 중단", {"day_pnl_pct": day_realized_pnl_pct})
                break

            # 신규 진입
            if pos.flat():
                qty = position_size(cash_usd, price)
                if qty <= 0:
                    log("잔고/리스크 제약으로 진입 불가", {"cash_usd": cash_usd, "calc_qty": qty})
                elif sig == "BUY":
                    resp = place_order("BUY", qty)
                    log("매수 진입", {"qty": qty, "price": price, "rsi": rsi})
                    log("주문 응답", resp)
                    pos.open("LONG", price, qty)
                elif sig == "SELL":
                    resp = place_order("SELL", qty)
                    log("매도 진입", {"qty": qty, "price": price, "rsi": rsi})
                    log("주문 응답", resp)
                    pos.open("SHORT", price, qty)
                else:
                    log("신호 없음: 대기")

            # 마감 근처 강제 청산 (근사)
            kst_now = now_kst()
            if kst_now.hour in (5, 6) and not pos.flat():
                log("장마감 근처 강제 청산")
                side = "SELL" if pos.side == "LONG" else "BUY"
                resp = place_order(side, pos.qty)
                log("청산 응답", resp)
                day_realized_pnl_pct += unrealized_pnl_pct(pos, price)
                pos.close()

            time.sleep(60)  # 1분 주기

        except requests.HTTPError as he:
            log("HTTP 오류", {"error": str(he)})
            time.sleep(10)
        except Exception as e:
            log("예외 발생", {"error": str(e)})
            time.sleep(5)


if __name__ == "__main__":
    run_bot()