import streamlit as st
import pandas as pd
import numpy as np
import re
import time # 스피너 효과를 위해 추가

# --- algo.ipynb의 스크래핑/분석 로직을 함수로 가져오기 ---
# (셀 3: 스크래핑)
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# (셀 5: 분석 로직에 필요한 키워드 및 함수들)
suspicious_keywords = [
    "단기임대", "저금리", "대출이자", "대출 알선", "실입주금", "실입주 금액",
    "당일계약", "계약 서두르세요", "보증금 대납"
]

include_keywords = {
    "수도": ["수도", "수도료"],
    "인터넷/TV": ["인터넷", "IPTV", "와이파이", "wifi"],
    "전기": ["전기세", "전기 요금", "전기", "공용전기"],
    "가스/난방": ["가스", "도시가스", "난방"],
    "청소/관리": ["청소", "청소비", "일반관리비", "관리비 포함"],
    "주차": ["주차 포함", "주차비 포함"],
    "엘리베이터/건물": ["엘리베이터", "건물유지비", "공용관리비"]
}

# --- 헬퍼 함수 (숫자 변환, 키워드 카운트 등) ---

def to_float(x):
    if pd.isna(x): return np.nan
    return float(str(x).replace(",", ""))

def calc_price_risk(row):
    risk = 0
    if pd.notna(row["보증금비율"]):
        if row["보증금비율"] <= 0.7: risk += 4
        elif row["보증금비율"] <= 0.8: risk += 2
    if pd.notna(row["월세비율"]):
        if row["월세비율"] <= 0.7: risk += 4
        elif row["월세비율"] <= 0.8: risk += 2
    return risk

# (앱 개선: 어떤 키워드가 걸렸는지도 반환하도록 수정)
def analyze_keywords(text):
    if pd.isna(text):
        return 0, []
    text = str(text)
    found_kws = [kw for kw in suspicious_keywords if kw in text]
    return len(found_kws), found_kws

def parse_manage_fee(manage_fee_str):
    if manage_fee_str is None or pd.isna(manage_fee_str): return np.nan
    text = str(manage_fee_str)
    if "확인불가" in text: return np.nan
    m = re.search(r"([\d\.]+)\s*만원", text)
    if not m: return np.nan
    return float(m.group(1)) * 10000

def extract_manage_includes(desc):
    if desc is None or pd.isna(desc): return []
    text = str(desc)
    found = []
    for label, kws in include_keywords.items():
        for kw in kws:
            if kw in text:
                found.append(label)
                break
    return list(set(found))

def calc_manage_fee_risk(manage_fee_str, desc):
    fee = parse_manage_fee(manage_fee_str)
    includes = extract_manage_includes(desc)
    cnt = len(includes)
    risk, label = 0, "정상"
    
    if fee is np.nan or pd.isna(fee):
        risk = 3; label = "위험"
    elif fee < 80000:
        risk = 0; label = "정상"
    elif fee < 110000:
        if cnt < 2: risk = 1; label = "주의"
    elif fee < 150000:
        if cnt < 3: risk = 2; label = "위험"
        else: risk = 1; label = "주의"
    else:
        if cnt < 4: risk = 3; label = "위험"
        else: risk = 2; label = "주의"
    
    return risk, label, includes, cnt

# --- @st.cache_resource: 웹드라이버는 한 번만 실행되도록 캐시 ---
@st.cache_resource
def get_driver():
    options = Options()
    options.add_argument("--headless") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Streamlit Cloud에 설치된 chromedriver의 경로를 직접 지정합니다.
    driver = webdriver.Chrome(
        service=Service('/usr/bin/chromedriver'), 
        options=options
    )
    return driver

# --- @st.cache_data: 'dong_ss.csv' 파일은 한 번만 읽도록 캐시 ---
@st.cache_data
def load_avg_data():
    try:
        return pd.read_csv("dong_ss.csv")
    except FileNotFoundError:
        st.error("오류: 'dong_ss.csv' 파일을 찾을 수 없습니다! app.py와 같은 폴더에 있어야 합니다.")
        return None

# --- 1. 스크래핑 함수 (URL 입력받기) ---
def scrape_zigbang_data(url, driver):
    driver.get(url)
    wait = WebDriverWait(driver, 10)
    
    # 1) 주소 + 관리비
    loc_text = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.css-1563yu1'))).text.strip()
    if " · " in loc_text:
        address, manage_fee = loc_text.split(" · ", 1)
    else:
        address, manage_fee = loc_text, None

    # 2) 페이지 전체 텍스트
    full = driver.find_element(By.TAG_NAME, "body").text
    
    # 3) 보증금 / 월세
    m = re.search(r"월세\s*([\d,]+)\s*/\s*([\d,]+)", full)
    deposit, rent = (m.group(1), m.group(2)) if m else (None, None)

    # 4) 전용면적
    area_match = re.search(r"전용\s*([\d\.]+)m²", full)
    area = area_match.group(1) if area_match else None

    # 5) 상세설명
    start_idx = None
    for key in ["상세 설명", "특징 및 기타 사항"]:
        if key in full: start_idx = full.index(key); break
    
    if start_idx is not None:
        desc_full = full[start_idx:]
        end_idx = desc_full.find("더보기")
        desc = desc_full[:end_idx].strip() if end_idx != -1 else desc_full.strip()
    else:
        desc = None

    row = {
        "주소": address, "관리비": manage_fee, "보증금": deposit,
        "월세": rent, "전용면적": area, "상세설명": desc
    }
    return pd.DataFrame([row])

# --- 2. 위험도 분석 함수 ---
def analyze_risk_data(df, avg_df):
    merged = df.copy()
    
    # 2. 동 추출
    merged["동"] = merged["주소"].str.extract(r"(\S+동)")
    
    # 3. 숫자 변환
    merged["보증금_num"] = merged["보증금"].apply(to_float)
    merged["월세_num"] = merged["월세"].apply(to_float)
    
    # 4. 평균 시세 merge
    merged = merged.merge(avg_df, on="동", how="left")
    
    # 5. 비율 계산
    merged["보증금비율"] = merged["보증금_num"] / merged["평균보증금"]
    merged["월세비율"] = merged["월세_num"] / merged["평균월세"]
    
    # 6. 가격 위험 점수
    merged["가격위험점수"] = merged.apply(calc_price_risk, axis=1)
    
    # 7. 키워드 위험 점수 (개선된 함수 적용)
    kw_results = merged["상세설명"].apply(analyze_keywords)
    merged["키워드위험개수"] = kw_results.apply(lambda x: x[0])
    merged["발견키워드"] = kw_results.apply(lambda x: x[1])

    # 8. 관리비 위험 점수
    manage_risks = merged.apply(
        lambda row: calc_manage_fee_risk(row["관리비"], row["상세설명"]), axis=1
    )
    merged["관리비위험점수"] = manage_risks.apply(lambda x: x[0])
    merged["관리비판정"] = manage_risks.apply(lambda x: x[1])
    merged["관리비포함항목"] = manage_risks.apply(lambda x: x[2])

    # 9. 총 위험 점수 & 등급 (※ 노트북 코드 수정: 관리비 점수도 총점에 포함!)
    merged["총위험점수"] = merged["가격위험점수"] + merged["키워드위험개수"] + merged["관리비위험점수"]
    
    merged["위험등급"] = pd.cut(
        merged["총위험점수"],
        bins=[-1, 3, 7, 12, 20],      
        labels=["낮음", "보통", "주의", "위험"]
    )
    
    return merged.iloc[0] # 첫 번째 (유일한) 행의 분석 결과를 반환

# ==========================================================
#  streamlit 앱 UI 부분
# ==========================================================

st.title("🕵️ 직방 매물 위험도 분석기")
st.write("분석하고 싶은 직방 원룸/오피스텔의 '공유하기' URL을 입력하세요.")

# 1. 'dong_ss.csv' 로드
avg_df = load_avg_data()

# 2. URL 입력창
url = st.text_input("직방 URL을 여기에 붙여넣으세요:", placeholder="https://sp.zigbang.com/share/oneroom/...")

# 3. 분석 버튼
if st.button("위험도 분석 시작하기 🚀") and avg_df is not None:
    if "zigbang.com" not in url:
        st.error("올바른 직방(zigbang.com) URL을 입력해주세요.")
    else:
        try:
            # 4. 스피너 실행 (로딩 중 표시)
            with st.spinner("매물 정보를 스크래핑하고 위험도를 분석 중입니다... 잠시만 기다려주세요."):
                driver = get_driver()
                scraped_df = scrape_zigbang_data(url, driver)
                result = analyze_risk_data(scraped_df, avg_df)
            
            st.success("🎉 분석이 완료되었습니다!")
            st.divider() # --- 구분선 ---

            # 5. 결과 표시 (요청하신 부분)
            
            # 5-1. 주소
            st.subheader(f"🏠 주소: {result['주소']}")
            
            # 5-2. 위험등급 및 총점
            level = result['위험등급']
            if level == '위험' or level == '주의':
                st.error(f"🚨 위험 등급: {level}")
            elif level == '보통':
                st.warning(f"⚠️ 위험 등급: {level}")
            else:
                st.success(f"✅ 위험 등급: {level}")
            
            st.metric(label="총 위험 점수", value=f"{result['총위험점수']} 점")
            
            
            # 5-3. 위험 점수 세부 내역 (점수가 생긴 분야)
            st.subheader("📈 위험 점수 세부 내역")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("💰 가격 점수", f"{result['가격위험점수']} 점")
            col2.metric("🔑 키워드 점수", f"{result['키워드위험개수']} 점")
            col3.metric("🧾 관리비 점수", f"{result['관리비위험점수']} 점")

            # 5-4. (추가) 왜 점수가 나왔는지 상세 설명
            if result['가격위험점수'] > 0:
                st.caption(f"  - 동네 평균 대비 가격이 낮습니다. (보증금 비율: {result['보증금비율']:.2f}, 월세 비율: {result['월세비율']:.2f})")
            
            if result['키워드위험개수'] > 0:
                st.caption(f"  - 상세설명에서 다음 위험 키워드가 발견되었습니다: **{', '.join(result['발견키워드'])}**")
            
            if result['관리비위험점수'] > 0:
                st.caption(f"  - 관리비가 {result['관리비']}이며 '{result['관리비판정']}' 판정을 받았습니다.")
            
            # 5-5. (참고) 전체 데이터 보여주기 (선택 사항)
            with st.expander("전체 분석 데이터 보기"):
                st.dataframe(result)
                
        except Exception as e:
            st.error(f"분석 중 오류가 발생했습니다: {e}")

            st.error("URL이 정확한지, 또는 직방의 페이지 구조가 변경되지 않았는지 확인해주세요.")
