# AMD RSI 자동매매 봇 (KIS OpenAPI)

이 프로젝트는 한국투자증권 OpenAPI를 활용하여 **AMD 주식 자동매매**를 수행하는 봇입니다.  
RSI 전략을 기반으로 매수/매도 신호를 판단하고, 모의투자 및 실투자 환경 모두 지원합니다.

---

## 🚀 주요 기능
- RSI 기반 자동매매 (1분봉)
- 모의투자 / 실투자 모드 전환 (`MODE=paper` 또는 `MODE=live`)
- 계좌 잔고 조회 및 대시보드 출력
- 보유 종목, 평가손익, 총 평가금액 조회
- 일일 손실 한도 관리 (-3% 기본값)
- 익절/손절 자동 청산
- 장 마감 근처 강제 청산
- 실행 로그 저장 (`bot_log.txt`)

---

## 📦 설치 방법

### 1. 환경 준비
- Python 3.9 이상 설치
- pip 패키지 설치:
  ```bash
  pip install requests pandas python-dotenv pytz
