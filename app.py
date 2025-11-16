import re
import numpy as np
import pandas as pd
import streamlit as st

# Selenium 관련
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ================================
# 0. 동네별 평균 시세 불러오기
# ================================
@st.cache_data
def load_avg_df():
    # dong_ss.csv 파일은 이 파일과 같은 폴더에 있다고 가정
    return pd.read_csv("dong_ss.csv")  # 컬럼: 동, 평균보증금, 평균월세

avg_df = load_avg_df()


# ================================
# 1. 직방 매물 1개 크롤링 함수
# ================================
def scrape_one_zigbang(url: str) -> dict:
    """
    직방 원룸 매물 URL을 받아서
    주소 / 관리비 / 보증금 / 월세 / 전용면적 / 상세설명을 딕셔너리로 반환
    (app.py 로직 기반)
    """
    options = Options()
    # 디버깅 다 끝나면 아래 주석을 풀고 headless 모드로 돌려도 됨
    # options.add_argument("--headless=new")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    try:
        driver.get(url)
        wait = WebDriverWait(driver, 10)

        # 1) 주소 + 관리비
        try:
            loc_text = wait.until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    'div.css-1563yu1.r-aw03qq.r-1wbh5a2.r-1w6e6rj.r-159m18f.r-1b43r93.r-16dba41.r-rjixqe'
                ))
            ).text.strip()
        except Exception:
            loc_text = ""

        # " · " 기준으로 분리 → "서울시 성북구 상월곡동  · 관리비 9.5만원"
        if " · " in loc_text:
            address, manage_fee = loc_text.split(" · ", 1)
        else:
            address = loc_text
            manage_fee = None

        # 2) 페이지 전체 텍스트
        full = driver.find_element(By.TAG_NAME, "body").text

        # 3) 보증금 / 월세  (예: "월세 1,000/53")
        m = re.search(r"월세\s*([\d,]+)\s*/\s*([\d,]+)", full)
        if m:
            deposit = m.group(1)
            rent = m.group(2)
        else:
            deposit = None
            rent = None

        # 4) 전용면적  (예: "전용 21.45m²")
        area_match = re.search(r"전용\s*([\d\.]+)m²", full)
        area = area_match.group(1) if area_match else None

        # 5) 상세 설명
        desc = None
        start_idx = None
        for key in ["상세 설명", "특징 및 기타 사항"]:
            if key in full:
                start_idx = full.index(key)
                break

        if start_idx is not None:
            desc_full = full[start_idx:]
            end_idx = desc_full.find("더보기")
            desc = desc_full[:end_idx].strip() if end_idx != -1 else desc_full.strip()

        return {
            "주소": address,
            "관리비": manage_fee,
            "보증금": deposit,
            "월세": rent,
            "전용면적": area,
            "상세설명": desc
        }

    finally:
        driver.quit()


# ================================
# 2. 위험도 계산 공통 함수들 (app.py 로직)
# ================================
def to_float(x):
    if pd.isna(x):
        return np.nan
    return float(str(x).replace(",", ""))


# 2-1) 가격 기반 위험 점수 (보증금/월세 각각 최대 4점)
def calc_price_risk(row):
    risk = 0
    # 보증금 기준
    if pd.notna(row["보증금비율"]):
        r = row["보증금비율"]
        if r <= 0.7:
            risk += 4
        elif r <= 0.8:
            risk += 2
    # 월세 기준
    if pd.notna(row["월세비율"]):
        r = row["월세비율"]
        if r <= 0.7:
            risk += 4
        elif r <= 0.8:
            risk += 2
    return risk


# 2-2) 상세설명 위험 키워드
suspicious_keywords = [
    "단기임대",
    "저금리", "대출이자", "대출 알선",
    "실입주금", "실입주 금액", "보증금 대납",
    "당일계약", "계약 서두르세요"
]


def count_keywords(text):
    if pd.isna(text):
        return 0
    text = str(text)
    return sum(1 for kw in suspicious_keywords if kw in text)


def find_keywords(text):
    if pd.isna(text):
        return []
    text = str(text)
    return [kw for kw in suspicious_keywords if kw in text]


# 2-3) 관리비 파싱 (예: "관리비 9.5만원" → 95000)
def parse_manage_fee(manage_fee_str):
    if manage_fee_str is None or pd.isna(manage_fee_str):
        return np.nan
    text = str(manage_fee_str)
    if "확인불가" in text:
        return np.nan
    m = re.search(r"([\d\.]+)\s*만원", text)
    if not m:
        return np.nan
    return float(m.group(1)) * 10000  # 만원 → 원


# 2-4) 상세설명에서 관리비 포함 항목 추출
include_keywords = {
    "수도": ["수도", "수도료"],
    "인터넷/TV": ["인터넷", "IPTV", "와이파이", "wifi"],
    "전기": ["전기세", "전기 요금", "전기", "공용전기"],
    "가스/난방": ["가스", "도시가스", "난방"],
    "청소/관리": ["청소", "청소비", "일반관리비", "관리비 포함"],
    "주차": ["주차 포함", "주차비 포함"],
    "엘리베이터/건물": ["엘리베이터", "건물유지비", "공용관리비"]
}


def extract_manage_includes(desc):
    if desc is None or pd.isna(desc):
        return []
    text = str(desc)
    found = []
    for label, kws in include_keywords.items():
        for kw in kws:
            if kw in text:
                found.append(label)
                break
    return list(set(found))


# 2-5) 관리비 위험도 계산
def calc_manage_fee_risk(manage_fee_str, desc):
    """
    관리비 금액 + 포함 항목 개수로
    위험점수(0~3), 판정("정상"/"주의"/"위험"), 포함항목리스트, 개수 반환
    """
    fee = parse_manage_fee(manage_fee_str)
    includes = extract_manage_includes(desc)
    cnt = len(includes)

    risk = 0
    label = "정상"

    # 관리비 금액이 없거나 확인불가 → 위험
    if fee is np.nan or pd.isna(fee):
        risk = 3
        label = "위험"
        return risk, label, includes, cnt

    if fee < 80000:
        risk = 0
        label = "정상"
    elif fee < 110000:
        if cnt >= 2:
            risk = 0
            label = "정상"
        else:
            risk = 1
            label = "주의"
    elif fee < 150000:
        if cnt >= 3:
            risk = 1
            label = "주의"
        else:
            risk = 2
            label = "위험"
    else:
        if cnt >= 4:
            risk = 2
            label = "주의"
        else:
            risk = 3
            label = "위험"

    return risk, label, includes, cnt


# ================================
# 3. 한 매물에 대한 설명 문장 생성 (app.py 로직)
# ================================
def analyze_one_item(row):
    """
    row: 결과 DataFrame의 한 행
    반환: (위험등급, [설명문 리스트])
    """
    msgs = []

    # 동 정보
    if pd.isna(row["동"]):
        msgs.append("주소에서 동 정보를 추출하지 못했습니다.")
    else:
        # 보증금/월세 차이 설명 (10% 이상 차이날 때만 문장 생성)
        if pd.notna(row["보증금비율"]):
            ratio = row["보증금비율"]
            diff = (1 - ratio) * 100  # 양수면 평균보다 싸다
            if abs(diff) >= 10:
                if diff > 0:
                    msgs.append(f"보증금이 {row['동']} 평균 보증금보다 약 {diff:.1f}% 저렴합니다.")
                else:
                    msgs.append(f"보증금이 {row['동']} 평균 보증금보다 약 {abs(diff):.1f}% 비쌉니다.")

        if pd.notna(row["월세비율"]):
            ratio = row["월세비율"]
            diff = (1 - ratio) * 100
            if abs(diff) >= 10:
                if diff > 0:
                    msgs.append(f"월세가 {row['동']} 평균 월세보다 약 {diff:.1f}% 저렴합니다.")
                else:
                    msgs.append(f"월세가 {row['동']} 평균 월세보다 약 {abs(diff):.1f}% 비쌉니다.")

    # 관리비 설명
    fee = parse_manage_fee(row["관리비"])
    if row["관리비판정"] == "위험":
        if pd.isna(fee):
            msgs.append("관리비가 '확인불가'로 표시되어 있어 위험도가 높습니다.")
        else:
            msgs.append(f"관리비가 {int(fee):,}원으로 높은 편이며, 포함 항목이 적어 '위험' 판정입니다.")
    elif row["관리비판정"] == "주의":
        msgs.append("관리비가 다소 높은 편이거나 포함 항목이 충분하지 않아 '주의' 판정입니다.")

    if row["관리비포함개수"] > 0:
        msgs.append(f"관리비에 포함된 항목: {', '.join(row['관리비포함항목'])}")

    # 상세설명 키워드
    kws = find_keywords(row["상세설명"])
    if len(kws) > 0:
        msgs.append(f"상세 설명에서 다음 위험 키워드가 발견되었습니다: {', '.join(kws)}")

    # 점수 요약
    msgs.append(
        f"가격위험점수: {row['가격위험점수']}, "
        f"키워드위험개수: {row['키워드위험개수']}, "
        f"관리비위험점수: {row['관리비위험점수']}, "
        f"총위험점수: {row['총위험점수']}점"
    )

    return row["위험등급"], msgs


# ================================
# 4. Streamlit UI (app2 스타일 적용)
# ================================
st.title("🕵️ 직방 매물 위험도 분석기")
st.write("분석하고 싶은 직방 원룸/오피스텔의 **공유하기 URL**을 입력하면, "
         "동네 평균 시세/관리비/위험 키워드를 기반으로 허위매물 위험도를 분석합니다.")

url = st.text_input(
    "직방 URL을 여기에 붙여넣으세요:",
    placeholder="https://sp.zigbang.com/share/oneroom/..."
)

if st.button("위험도 분석 시작하기 🚀"):
    if not url.strip():
        st.error("직방 URL을 입력해주세요.")
    elif "zigbang.com" not in url:
        st.error("올바른 직방(zigbang.com) URL을 입력해주세요.")
    else:
        try:
            with st.spinner("매물 정보를 크롤링하고 위험도를 분석 중입니다... 잠시만 기다려주세요."):
                # 1) 크롤링
                item = scrape_one_zigbang(url)
                df = pd.DataFrame([item])

                # 2) 동 추출
                df["동"] = df["주소"].str.extract(r"(\S+동)")

                # 3) 숫자 변환
                df["보증금_num"] = df["보증금"].apply(to_float)
                df["월세_num"] = df["월세"].apply(to_float)

                # 4) 평균 시세 merge
                merged = df.merge(avg_df, on="동", how="left")

                # 5) 비율 계산
                merged["보증금비율"] = merged["보증금_num"] / merged["평균보증금"]
                merged["월세비율"] = merged["월세_num"] / merged["평균월세"]

                # 6) 가격 위험 점수
                merged["가격위험점수"] = merged.apply(calc_price_risk, axis=1)

                # 7) 키워드 위험 (개수만)
                merged["키워드위험개수"] = merged["상세설명"].apply(count_keywords)

                # 8) 관리비 위험
                manage_risks = merged.apply(
                    lambda row: calc_manage_fee_risk(row["관리비"], row["상세설명"]),
                    axis=1
                )
                merged["관리비위험점수"] = manage_risks.apply(lambda x: x[0])
                merged["관리비판정"] = manage_risks.apply(lambda x: x[1])
                merged["관리비포함항목"] = manage_risks.apply(lambda x: x[2])
                merged["관리비포함개수"] = manage_risks.apply(lambda x: x[3])

                # 9) 총위험점수 & 등급
                merged["총위험점수"] = (
                    merged["가격위험점수"].fillna(0)
                    + merged["키워드위험개수"].fillna(0)
                    + merged["관리비위험점수"].fillna(0)
                )

                # 점수 구간: 0~3 낮음, 4~7 보통, 8~12 주의, 13 이상 위험 (app.py 기준)
                merged["위험등급"] = pd.cut(
                    merged["총위험점수"],
                    bins=[-1, 3, 7, 12, 100],
                    labels=["낮음", "보통", "주의", "위험"]
                )

            row = merged.iloc[0]
            등급, 설명들 = analyze_one_item(row)

            st.success("🎉 분석이 완료되었습니다!")
            st.divider()

            # ============================
            # 5. UI 출력 (app2 스타일)
            # ============================

            # 5-1. 매물 기본 정보
            st.subheader("😀 매물 기본 정보")
            st.write(f"**주소** : {row['주소']}")
            st.write(f"**동** : {row['동']}")
            st.write(f"**보증금 / 월세** : {row['보증금']} / {row['월세']}")
            st.write(f"**관리비** : {row['관리비']}")
            st.write(f"**전용면적** : {row['전용면적']} m²")

            # 5-2. 위험등급 UI
            st.subheader("🔎 허위매물 위험도 결과")

            if 등급 == "위험" or 등급 == "주의":
                st.error(f"🚨 위험 등급: {등급}")
            elif 등급 == "보통":
                st.warning(f"⚠️ 위험 등급: {등급}")
            else:
                st.success(f"✅ 위험 등급: {등급}")

            st.metric(label="총 위험 점수", value=f"{row['총위험점수']} 점")

            # 5-3. 점수 세부 내역
            st.subheader("📈 위험 점수 세부 내역")
            col1, col2, col3 = st.columns(3)
            col1.metric("💰 가격 점수", f"{row['가격위험점수']} 점")
            col2.metric("🔑 키워드 점수", f"{row['키워드위험개수']} 점")
            col3.metric("🧾 관리비 점수", f"{row['관리비위험점수']} 점")

            # 5-4. 설명 문장 (app.py의 analyze_one_item 활용)
            st.subheader("📋 상세 분석 내용")
            for msg in 설명들:
                st.markdown(f"- {msg}")

            # 5-5. 디버깅/참고용 전체 데이터
            with st.expander("🔍 전체 분석 데이터(디버깅용) 보기"):
                st.dataframe(merged)

        except Exception as e:
            st.error(f"분석 중 오류가 발생했습니다: {e}")
            st.error("URL이 정확한지, 또는 직방 페이지 구조/크롬 드라이버 환경을 확인해주세요.")

