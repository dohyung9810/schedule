# app.py — Streamlit 직원/스케줄 데모 (XLSX 업/다운, 휴무일/가동일, 5인판정, 모달 호환, 업로드 무한루프 방지)
# 실행:  streamlit run app.py

import io
import calendar
from datetime import date
from typing import List, Dict

import pandas as pd
import streamlit as st




# ----------------- Streamlit rerun 헬퍼 -----------------
def do_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


# ----------------- 상수 -----------------
KOREAN_DAYS = ["월", "화", "수", "목", "금", "토", "일"]
EMP_TYPES = ["4대보험", "초단시간", "사업소득", "일용직"]


# ----------------- 세션 초기화 -----------------
def _ensure_state():
    ss = st.session_state
    ss.setdefault("employees", [])        # [{name, phone, role, employment_type, available_days:[...]}]
    ss.setdefault("assignments", {})      # {"YYYY-MM-DD":[{name, employment_type, clock_in, clock_out, break, wage}]}
    ss.setdefault("closed", {})           # {"YYYY-MM": {day:int -> 1}}
    ss.setdefault("_open_day_req", "")    # 모달 트리거(일자)
    ss.setdefault("_closed_req", None)    # 모달 트리거(휴무관리: (y,m))
    # 업로드 무한루프 방지용
    ss.setdefault("upload_token", None)   # 마지막 처리한 파일 식별자
    ss.setdefault("uploader_key", 0)      # 업로더 리셋용 키


_ensure_state()


# ----------------- 유틸 -----------------
def ymd(y, m, d) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def ym(y, m) -> str:
    return f"{int(y):04d}-{int(m):02d}"


def _clean_colname(s: str) -> str:
    # 소문자, 공백/특수문자 제거
    import re
    s = str(s or "").strip().lower()
    s = re.sub(r"[\s_\-()/\[\]{}·.]+", "", s)
    return s


# 다양한 헤더(한글/영문/변형)를 표준키로 매핑
HEADER_MAP = {
    "name": ["name", "이름", "성명"],
    "phone": ["phone", "연락처", "전화", "전화번호", "휴대폰", "핸드폰", "mobile"],
    "role": ["role", "포지션", "메모", "직무", "직책", "비고"],
    "employment_type": ["employmenttype", "고용형태", "고용", "형태", "구분", "신분"],
    "available_days": ["availabledays", "가용요일", "근무요일", "요일", "가능요일"],
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    norm = {_clean_colname(c): c for c in df.columns}
    out = pd.DataFrame()
    for target_key, aliases in HEADER_MAP.items():
        hit_col = None
        for alias in aliases:
            key = _clean_colname(alias)
            if key in norm:
                hit_col = norm[key]
                break
        if hit_col is not None:
            out[target_key] = df[hit_col]
        else:
            out[target_key] = ""
    return out


def normalize_days(raw):
    """
    허용 예시:
      - "월,수,금"
      - "월/수/금"
      - "월 수 금"
      - "월수금" (붙여쓴 형태)
      - "월ㆍ수ㆍ금", "월·수·금"
    """
    if pd.isna(raw):
        return []
    s = str(raw).strip()
    if not s:
        return []

    # 구분자 통일
    for sep in ["|", "/", ";", " ", "·", "ㆍ"]:
        s = s.replace(sep, ",")

    if "," not in s:
        # 붙여쓴 표현 -> 문자 단위로 쪼개서 요일만 추출
        chars = list(s)
        parts = []
        buf = ""
        for ch in chars:
            buf += ch
            if ch in KOREAN_DAYS:
                parts.append(buf)
                buf = ""
        if buf:
            parts.append(buf)
    else:
        parts = [p.strip() for p in s.split(",") if p.strip()]

    days = []
    for p in parts:
        for d in KOREAN_DAYS:
            if d in p:
                days.append(d)
                break

    # 중복 제거(순서 유지)
    seen = set()
    out = []
    for d in days:
        if d not in seen:
            out.append(d)
            seen.add(d)
    return out


def employees_to_df(employees: List[Dict]) -> pd.DataFrame:
    rows = []
    for e in employees:
        rows.append({
            "name": e.get("name", ""),
            "phone": e.get("phone", ""),
            "role": e.get("role", ""),
            "employment_type": e.get("employment_type", ""),
            "available_days": ",".join(e.get("available_days", [])),
        })
    return pd.DataFrame(rows, columns=["name", "phone", "role", "employment_type", "available_days"])


def df_to_employees(df: pd.DataFrame) -> (List[Dict], List):
    # 1) 헤더 정규화/매핑
    df = normalize_columns(df)

    added = []
    skipped_info = []  # (row_index, reason)

    for idx, r in df.iterrows():
        # 사람 기준 행번호(1부터) + 헤더 1줄 = +2
        row_no = idx + 2

        name = str(r.get("name", "")).strip()
        if not name:
            skipped_info.append((row_no, "이름(name) 없음"))
            continue

        et_raw = str(r.get("employment_type", "")).strip()
        et = et_raw if et_raw in EMP_TYPES else EMP_TYPES[0]

        days = normalize_days(r.get("available_days", ""))

        added.append({
            "name": name,
            "phone": str(r.get("phone", "")).strip(),
            "role": str(r.get("role", "")).strip(),
            "employment_type": et,
            "available_days": days,
        })

    return added, skipped_info


# XLSX 바이트로 변환
def df_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    # openpyxl 또는 xlsxwriter 둘 중 하나만 설치돼 있어도 OK
    engine = None
    try:
        import openpyxl  # noqa
        engine = "openpyxl"
    except Exception:
        try:
            import xlsxwriter  # noqa
            engine = "xlsxwriter"
        except Exception:
            engine = None
    if engine is None:
        raise RuntimeError("XLSX 저장을 위해 openpyxl 또는 xlsxwriter가 필요합니다. pip install openpyxl")

    with pd.ExcelWriter(out, engine=engine) as writer:
        df.to_excel(writer, index=False, sheet_name="employees")
    out.seek(0)
    return out.getvalue()


# ----------------- 계산 -----------------
def minutes_between(cin: str, cout: str, brk: int) -> int:
    def to_min(hhmm):
        hh, mm = hhmm.split(":")
        return int(hh) * 60 + int(mm)
    return max(0, to_min(cout) - to_min(cin) - max(0, int(brk or 0)))


def shift_cost(cin, cout, brk, wage) -> float:
    mins = minutes_between(cin, cout, brk)
    return (mins / 60.0) * float(wage or 0)


# ----------------- 자동 배치 (휴무 스킵 + 요일 매핑 정확) -----------------
def auto_assign_for_month(year: int, month: int):
    closed = st.session_state.closed.get(ym(year, month), {})
    cal = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    for wk in cal:
        for d in wk:
            if d == 0 or closed.get(d):
                continue
            key = ymd(year, month, d)
            # 월=0..일=6 (정확 매핑)
            wday = KOREAN_DAYS[date(year, month, d).weekday()]
            candidates = [e for e in st.session_state.employees if wday in (e.get("available_days") or [])]
            if not candidates:
                continue
            day_list = st.session_state.assignments.setdefault(key, [])
            exists = {r["name"] for r in day_list}
            for emp in candidates:
                if emp["name"] in exists:
                    continue
                day_list.append({
                    "name": emp["name"],
                    "employment_type": emp.get("employment_type", ""),
                    "clock_in": "09:00",
                    "clock_out": "18:00",
                    "break": 60,
                    "wage": 10000
                })
                exists.add(emp["name"])


# ----------------- 5인 이상/미만 (휴무일 제외 가동일 기준) -----------------
def biz_flag_for_month(year: int, month: int):
    closed = st.session_state.closed.get(ym(year, month), {})
    last = calendar.monthrange(year, month)[1]
    operating = 0
    meet = 0
    for d in range(1, last + 1):
        if closed.get(d):
            continue
        operating += 1
        key = ymd(year, month, d)
        arr = st.session_state.assignments.get(key, [])
        uniq = {r["name"] for r in arr if r.get("employment_type") != "사업소득"}
        if len(uniq) >= 5:
            meet += 1
    denom = max(1, operating)
    flag = "5인 이상" if meet >= (denom / 2) else "5인 미만"
    return flag, meet, denom


# ----------------- 모달 콘텐츠: 날짜 배치/추가 -----------------
def render_day_detail(body, day_key: str):
    body.subheader(f"{day_key} 배치 / 추가")

    day_list = st.session_state.assignments.get(day_key, [])
    if not day_list:
        body.info("현재 배치가 없습니다.")
    else:
        df = pd.DataFrame([
            {"이름": r["name"], "형태": r.get("employment_type", ""), "출근": r.get("clock_in", ""),
             "퇴근": r.get("clock_out", ""), "휴게(분)": r.get("break", 0), "시급": r.get("wage", 0)}
            for r in day_list
        ])
        body.dataframe(df, use_container_width=True, hide_index=True)
        # 간단 삭제 UI
        for idx, r in enumerate(day_list):
            ca, cb = body.columns([0.8, 0.2])
            ca.markdown(
                f"- {r['name']} · {r.get('clock_in', '')}~{r.get('clock_out', '')} "
                f"(휴게 {r.get('break', 0)}분, 시급 {r.get('wage', 0)})"
            )
            if cb.button("삭제", key=f"del-{day_key}-{idx}"):
                day_list.pop(idx)
                if day_list:
                    st.session_state.assignments[day_key] = day_list
                else:
                    st.session_state.assignments.pop(day_key, None)
                do_rerun()

    body.markdown("---")
    body.subheader("근무자 추가")

    with body.form(f"add-form-{day_key}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        emp_names = [e["name"] for e in st.session_state.employees]
        if emp_names:
            name_sel = c1.selectbox("직원 선택", options=emp_names, index=0)
            emp = next((e for e in st.session_state.employees if e["name"] == name_sel), None)
            emp_type_default = emp["employment_type"] if emp else EMP_TYPES[0]
        else:
            name_sel = c1.text_input("직원 이름 직접 입력*", placeholder="예: 신규직원")
            emp_type_default = EMP_TYPES[0]
        emp_type = c2.selectbox("고용형태", EMP_TYPES, index=EMP_TYPES.index(emp_type_default))

        c3, c4 = st.columns(2)
        clock_in = c3.time_input("출근", value=pd.to_datetime("09:00").time())
        clock_out = c4.time_input("퇴근", value=pd.to_datetime("18:00").time())

        c5, c6 = st.columns(2)
        brk = c5.number_input("휴게(분)", min_value=0, step=5, value=60)
        wage = c6.number_input("시급(원)", min_value=0, step=100, value=10000)

        submitted = st.form_submit_button("저장", use_container_width=True)
        if submitted:
            if emp_names:
                name_final = name_sel
            else:
                name_final = (name_sel or "").strip()

            if not name_final:
                st.warning("직원 이름을 입력/선택하세요.")
            else:
                # 직원 목록에 없으면 자동 등록 (+해당 날짜 요일을 기본 가용 요일로 추가)
                exists_emp = next((e for e in st.session_state.employees if e["name"] == name_final), None)
                if not exists_emp:
                    y, m, d = map(int, day_key.split("-"))
                    wday = KOREAN_DAYS[date(y, m, d).weekday()]
                    st.session_state.employees.append({
                        "name": name_final, "phone": "", "role": "",
                        "employment_type": emp_type, "available_days": [wday]
                    })

                item = {
                    "name": name_final,
                    "employment_type": emp_type,
                    "clock_in": f"{clock_in.hour:02d}:{clock_in.minute:02d}",
                    "clock_out": f"{clock_out.hour:02d}:{clock_out.minute:02d}",
                    "break": int(brk),
                    "wage": int(wage),
                }
                st.session_state.assignments.setdefault(day_key, []).append(item)
                st.success("추가되었습니다.")
                do_rerun()

    body.button("닫기", on_click=do_rerun)


# ----------------- 페이지 시작 -----------------
st.set_page_config(page_title="직원 · 스케줄", layout="wide")
st.title("👥 직원 · 스케줄")
st.markdown(
    """
    ## 사용 방법
    - 좌측에서 직원 등록 또는 XLSX 업로드
    - 우측에서 휴무일 관리 → 근무자 자동 배치
    - 날짜 카드의 **`추가`** 버튼으로 일자별 배치/수정
    - **5인 판정:** 휴무 제외 가동일 기준, 사업소득 제외, 50% 이상이면 5인 이상
    """
)
left, right = st.columns([0.45, 0.55])

# ---- 왼쪽: 직원 등록/업로드/다운로드/목록 ----
with left:
    st.subheader("직원 등록")
    with st.form("emp_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("이름*", placeholder="예: 홍길동")
        phone = c2.text_input("연락처(선택)", placeholder="01012345678")
        role = st.text_input("포지션/메모(선택)", placeholder="홀서빙 / 파트타이머 등")
        c3, c4 = st.columns(2)
        emp_type = c3.selectbox("고용형태", EMP_TYPES, index=0)
        days = c4.multiselect("가용 요일", KOREAN_DAYS, default=[])
        add_ok = st.form_submit_button("＋ 직원 추가", use_container_width=True)
    if add_ok:
        if not name.strip():
            st.warning("이름은 필수입니다.")
        else:
            st.session_state.employees.append({
                "name": name.strip(),
                "phone": phone.strip(),
                "role": role.strip(),
                "employment_type": emp_type,
                "available_days": days,
            })
            st.success(f"직원 '{name}' 추가 완료")
            do_rerun()

    st.divider()
    st.subheader("직원 업로드 / 다운로드 (XLSX)")
    st.caption("필드: name, phone, role, employment_type, available_days  / 예: available_days = 월,수,금")

    upcol1, upcol2 = st.columns([0.6, 0.4])
    with upcol1:
        tmpl_df = pd.DataFrame([{
            "name": "홍길동", "phone": "01012345678", "role": "홀서빙",
            "employment_type": "4대보험", "available_days": "월,수,금"
        }])
        st.download_button(
            "📥 업로드용 템플릿(XLSX)",
            data=df_to_xlsx_bytes(tmpl_df),
            file_name="employees_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with upcol2:
        cur_df = employees_to_df(st.session_state.employees)
        st.download_button(
            "⬇️ 현재 직원 목록(XLSX)",
            data=df_to_xlsx_bytes(cur_df),
            file_name="employees_current.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    # --------- 업로드 (무한 루프 방지 버전) ---------
    uploaded = st.file_uploader(
        "XLSX 업로드",
        type=["xlsx"],
        key=f"uploader-{st.session_state.uploader_key}"  # 위젯 리셋용 키
    )

    if uploaded is not None:
        # 1) 파일 고유 식별자 생성
        file_id = getattr(uploaded, "file_id", None)
        content = None
        if file_id is None:
            content = uploaded.getvalue()
            import hashlib
            file_id = hashlib.md5(content).hexdigest()

        # 2) 이미 처리한 파일인지 체크
        if st.session_state.upload_token == file_id:
            st.info("이미 처리한 파일입니다. 다른 파일을 업로드하세요.")
        else:
            with st.spinner("엑셀 처리중…"):
                if content is None:
                    content = uploaded.getvalue()

                # 원본 미리보기
                df_raw = pd.read_excel(io.BytesIO(content))
                with st.expander("업로드 원본 미리보기", expanded=False):
                    st.dataframe(df_raw.head(20), use_container_width=True)

                # 표준화 변환
                new_emps, skipped = df_to_employees(df_raw)

                if new_emps:
                    st.session_state.employees.extend(new_emps)
                    st.success(f"업로드 완료: {len(new_emps)}명 추가")
                    with st.expander("추가된 직원 미리보기", expanded=False):
                        st.dataframe(pd.DataFrame(new_emps), use_container_width=True, hide_index=True)
                else:
                    st.error("유효한 직원 레코드를 찾지 못했습니다. (이름은 필수)")

                if skipped:
                    st.warning(
                        "일부 행이 스킵되었습니다:\n" +
                        "\n".join([f"- {row}행: {reason}" for row, reason in skipped])
                    )

                # 3) 같은 파일 반복 처리 방지 토큰 저장
                st.session_state.upload_token = file_id

                # (선택) 업로더를 곧바로 비워 새 파일을 넣고 싶다면:
                # st.session_state.uploader_key += 1
                # do_rerun()

    st.divider()
    st.subheader("직원 목록")
    if not st.session_state.employees:
        st.info("아직 등록된 직원이 없습니다.")
    else:
        for i, e in enumerate(st.session_state.employees):
            with st.container(border=True):
                top = st.columns([0.8, 0.2])
                top[0].markdown(f"**{e['name']}** · {e.get('employment_type','')}")
                if top[1].button("삭제", key=f"emp-del-{i}"):
                    name_del = e["name"]
                    st.session_state.employees.pop(i)
                    # 이 직원 배정 제거
                    for k, arr in list(st.session_state.assignments.items()):
                        st.session_state.assignments[k] = [r for r in arr if r["name"] != name_del]
                        if not st.session_state.assignments[k]:
                            st.session_state.assignments.pop(k, None)
                    do_rerun()
                st.caption(f"연락처: {e.get('phone') or '-'} / 포지션: {e.get('role') or '-'}")
                days_badge = e.get("available_days") or []
                if days_badge:
                    st.markdown(
                        "<div>" + "".join(
                            f"<span style='display:inline-block;margin:0 6px 6px 0;padding:4px 10px;"
                            "border:1px solid #e2e8f0;border-radius:999px;background:#f8fafc;font-size:12px;"
                            "color:#334155'>" + d + "</span>"
                            for d in days_badge
                        ) + "</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.caption("가용 요일: -")

# ---- 오른쪽: 휴무/캘린더/배치/판정 ----
with right:
    st.subheader("월간 캘린더")
    today = date.today()
    c1, c2 = st.columns(2)
    year = c1.number_input("년도", value=today.year, min_value=2000, max_value=2100, step=1)
    month = c2.number_input("월", value=today.month, min_value=1, max_value=12, step=1)

    # 휴무일 관리 버튼(1회성 트리거) + 자동배치
    bcols = st.columns([0.5, 0.5])
    if bcols[0].button("휴무일 관리", use_container_width=True):
        st.session_state["_closed_req"] = (int(year), int(month))
        do_rerun()
    if bcols[1].button("근무자 자동 배치 (가용 요일/휴무 제외)", use_container_width=True):
        auto_assign_for_month(int(year), int(month))
        st.success("자동 배치 완료")
        do_rerun()

    # 5인 판정 배너 (휴무일 제외 가동일 기준)
    flag, meet, denom = biz_flag_for_month(int(year), int(month))
    st.markdown(
        f"""
        <div style="padding:10px 12px;border:1px solid #e2e8f0;border-radius:12px;background:#fff7ed;margin:10px 0 12px 0">
          <strong>사업장 판정:</strong> {flag} ({meet}/{denom})
          <span style="color:#64748b"> — 사업소득 제외, <u>휴무일 제외 가동일수</u> 기준 50% 이상이면 '5인 이상'</span>
        </div>
        """,
        unsafe_allow_html=True
    )

    # 요일 헤더
    hcols = st.columns(7)
    for i, label in enumerate(["월", "화", "수", "목", "금", "토", "일"]):
        hcols[i].markdown(f"**{label}**")

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(int(year), int(month))
    closed_map = st.session_state.closed.get(ym(int(year), int(month)), {})

    for wk in weeks:
        row = st.columns(7, gap="small")
        for i, d in enumerate(wk):
            if d == 0:
                # 빈 날짜 — 테두리 없음
                row[i].markdown("<div style='height:0'></div>", unsafe_allow_html=True)
                continue

            box = row[i].container(border=True)
            day_key = ymd(year, month, d)
            assigned = st.session_state.assignments.get(day_key, [])
            is_closed = bool(closed_map.get(d))

            title = f"**{d}일**"
            if is_closed:
                title += " <span style='color:#ef4444'>(휴무)</span>"
            box.markdown(title, unsafe_allow_html=True)
            box.caption(f"배치: {len(assigned)}명")

            # 1회성 트리거로 모달 오픈
            def _req_open(day):
                st.session_state["_open_day_req"] = day

            box.button(
                "추가",
                key=f"add-{day_key}",
                use_container_width=True,
                disabled=is_closed,
                on_click=_req_open,
                args=(day_key,),
            )

# ----- 휴무일 모달 (1회성 트리거) -----
_req = st.session_state.pop("_closed_req", None)
if _req:
    _y, _m = _req
    _title = f"{_y}-{_m:02d} 휴무일 관리"

    def _render_closed(body):
        body.subheader(_title)
        key = ym(_y, _m)
        last = calendar.monthrange(_y, _m)[1]
        picked = dict(st.session_state.closed.get(key, {}))  # copy

        cols = st.columns(7)
        for d in range(1, last + 1):
            c = cols[(d - 1) % 7]
            checked = bool(picked.get(d))
            if c.checkbox(f"{d}일", value=checked, key=f"closed-{key}-{d}"):
                picked[d] = 1
            else:
                picked.pop(d, None)

        st.markdown("---")
        sc = st.columns([1, 1])
        if sc[0].button("저장"):
            st.session_state.closed[key] = picked
            st.success("저장되었습니다.")
            do_rerun()
        if sc[1].button("닫기"):
            do_rerun()

    dlg = getattr(st, "dialog", None)
    xdlg = getattr(st, "experimental_dialog", None)
    used = False
    if callable(dlg):
        try:
            cm = dlg(_title, width="large")
            if hasattr(cm, "__enter__"):
                with cm:
                    _render_closed(st)
                used = True
            else:
                @dlg(_title)
                def _dlg():
                    _render_closed(st)
                _dlg(); used = True
        except TypeError:
            pass
    if not used and callable(xdlg):
        @xdlg(_title)
        def _xd():
            _render_closed(st)
        _xd(); used = True
    if not used:
        st.sidebar.header(_title)
        _render_closed(st.sidebar)

# ---- '추가' 모달 (1회성 트리거) ----
day_req = st.session_state.pop("_open_day_req", "")
if day_req:
    title = f"{day_req} - 근무자 추가"

    dlg = getattr(st, "dialog", None)
    xdlg = getattr(st, "experimental_dialog", None)
    used = False

    if callable(dlg):
        try:
            cm = dlg(title, width="large")
            if hasattr(cm, "__enter__"):
                with cm:
                    render_day_detail(st, day_req)
                used = True
            else:
                @dlg(title)
                def _show():
                    render_day_detail(st, day_req)
                _show(); used = True
        except TypeError:
            pass

    if not used and callable(xdlg):
        @xdlg(title)
        def _xshow():
            render_day_detail(st, day_req)
        _xshow(); used = True

    if not used:
        st.sidebar.header(title)
        render_day_detail(st.sidebar, day_req)
