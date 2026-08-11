"""
StockAI · AI股票分析看板（Streamlit 版 v4 —— 分页导航版）

左侧导航分页：总览 / 异动雷达 / 风险事件 / AI关注清单 / 股票详情
所有功能保留：AI Stock Brief、异动检测、新闻联动、风险日历、K线图（辅助，勾选后加载）

数据来源：
- 行情：Twelve Data（免费，注意每分钟只有8次额度，已做省额度设计）
- 新闻/财报日期：yfinance（免费，无需key）
- AI分析：Google Gemini（免费额度，没填key则自动隐藏AI相关内容，不影响其他功能）
"""

import json
import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf

st.set_page_config(page_title="StockAI · AI股票分析看板", layout="wide", page_icon="🧠")

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META"]
TWELVE_KEY = st.secrets.get("TWELVE_DATA_API_KEY", "")
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"

# ---------------------------------------------------------------------------
# 数据获取（带缓存，保护免费额度；同一次页面加载内重复调用同一支股票不会重复消耗额度）
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_quote(symbol: str):
    r = requests.get("https://api.twelvedata.com/quote",
                      params={"symbol": symbol, "apikey": TWELVE_KEY}, timeout=15)
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "查询失败，股票代码可能不存在"))
    return data


@st.cache_data(ttl=300, show_spinner=False)
def get_time_series(symbol: str, interval: str = "30min", outputsize: int = 60):
    r = requests.get("https://api.twelvedata.com/time_series",
                      params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVE_KEY},
                      timeout=15)
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "无法获取历史数据"))
    values = data.get("values", [])
    if not values:
        raise RuntimeError("没有历史数据（可能当前休市，或代码不存在）")
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("datetime").reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def get_news(symbol: str):
    items = []
    try:
        raw = yf.Ticker(symbol).news or []
    except Exception:
        raw = []
    for item in raw[:6]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title", "")
        src = content["provider"].get("displayName", "") if isinstance(content.get("provider"), dict) else item.get("publisher", "")
        url = content["canonicalUrl"].get("url", "") if isinstance(content.get("canonicalUrl"), dict) else item.get("link", "")
        pub_time = content.get("pubDate") or ""
        if title:
            items.append({"title": title, "src": src or "Yahoo Finance", "url": url, "time": pub_time})
    return items


@st.cache_data(ttl=3600, show_spinner=False)
def get_earnings_date(symbol: str):
    try:
        cal = yf.Ticker(symbol).calendar
        if isinstance(cal, dict) and cal.get("Earnings Date"):
            return str(cal["Earnings Date"][0])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 技术分析（K线辅助区用）
# ---------------------------------------------------------------------------

def compute_sma(series: pd.Series, window: int = 5):
    return series.rolling(window=window, min_periods=1).mean()


def compute_support_resistance(df: pd.DataFrame, n: int = 2, min_gap_pct: float = 0.006):
    closes = df["close"].values
    maxima, minima = [], []
    for i in range(1, len(closes) - 1):
        if closes[i] > closes[i - 1] and closes[i] > closes[i + 1]:
            maxima.append(closes[i])
        if closes[i] < closes[i - 1] and closes[i] < closes[i + 1]:
            minima.append(closes[i])
    if len(closes):
        maxima += [closes[0], closes[-1]]
        minima += [closes[0], closes[-1]]

    def dedupe(vals, reverse):
        vals = sorted(set(vals), reverse=reverse)
        out = []
        for v in vals:
            if all(abs(v - o) / o > min_gap_pct for o in out):
                out.append(v)
            if len(out) >= n:
                break
        return out

    return dedupe(maxima, True), dedupe(minima, False)


def nearest_bar_index(news_time_str: str, bar_times: pd.Series):
    try:
        t = pd.to_datetime(news_time_str, utc=True, errors="coerce")
        if pd.isna(t):
            return None
        bt = pd.to_datetime(bar_times, utc=True, errors="coerce")
        diffs = (bt - t).abs()
        if diffs.isna().all():
            return None
        return diffs.idxmin()
    except Exception:
        return None


def vol_ratio_from_quote(q):
    """直接用quote返回的volume/average_volume算比例，不用额外调time_series（省API额度）。"""
    try:
        vol = float(q.get("volume") or 0)
        avg = float(q.get("average_volume") or 0)
        if avg > 0:
            return vol / avg, avg
    except Exception:
        pass
    return 1.0, 0


def anomaly_score(pct, vol_ratio):
    """异动分数：涨跌幅 + 成交量异常加权，用来排"提前发现异动"的优先级。"""
    return abs(pct) + max(0, vol_ratio - 1) * 4


def signal_score(pct, vol_ratio):
    """综合信号分（0-100，非AI生成，纯数学计算：涨跌幅+成交量放大程度）。"""
    base = 50 + pct * 3
    boost = (vol_ratio - 1) * 15 if vol_ratio > 1 else 0
    score = base + (boost if pct >= 0 else -boost)
    return max(1, min(99, round(score)))


# ---------------------------------------------------------------------------
# AI Stock Brief（Gemini，免费额度，未设置key或调用失败时自动跳过）
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def get_ai_brief(symbol, name, price, change_pct, vol_ratio, heavy, news_titles, earn_date):
    if not GEMINI_KEY:
        return None
    vol_desc = f"为均量的 {vol_ratio:.1f} 倍（{'明显放量' if heavy else '量能偏低或持平'}）"
    news_block = "\n".join(f"- {t}" for t in news_titles) if news_titles else "（今天没有抓到相关新闻）"
    prompt = f"""你是一位中立、谨慎的金融数据助理。只根据下面提供的数据做客观解读，不要编造没有依据的信息，不要给出买卖建议，不要做确定性的未来预测（关于未来的表述要用"如果...可能..."这类概率性说法）。用简体中文回答。

数据：
股票代码：{symbol}（{name}）
现价：${price:.2f}，今日涨跌幅：{change_pct:+.2f}%
成交量：{vol_desc}
今日相关新闻标题：
{news_block}
即将到来的财报日期：{earn_date or '暂无数据'}

请只输出一个JSON对象，不要有任何其他文字、不要markdown代码块符号，格式严格如下：
{{"summary": "两三句话的整体摘要", "reason": "结合成交量和新闻分析可能的原因；如果证据不足要明确说'数据有限，仅供参考'", "watch": ["值得关注的点1（简短一句话）", "值得关注的点2"]}}"""
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=25,
        )
        data = r.json()

        if isinstance(data, dict) and "error" in data:
            err = data["error"]
            msg = err.get("message", str(err))
            reason = ""
            for detail in err.get("details", []):
                if detail.get("reason"):
                    reason = f"（reason: {detail['reason']}）"
                    break
            return {"error": f"[HTTP {r.status_code}] {msg} {reason}"}

        candidates = data.get("candidates") or []
        if not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason", "未知")
            return {"error": f"没有返回结果，原因：{reason}"}

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return {"error": f"返回内容为空，finishReason：{candidates[0].get('finishReason', '未知')}"}

        text = parts[0].get("text", "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 状态初始化
# ---------------------------------------------------------------------------

if "page" not in st.session_state:
    st.session_state.page = "overview"
if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = DEFAULT_WATCHLIST[0]
if "extra_symbols" not in st.session_state:
    st.session_state.extra_symbols = []
if "theme" not in st.session_state:
    st.session_state.theme = "深色"


def goto_stock(sym):
    st.session_state.current_symbol = sym
    st.session_state.page = "stock"
    if sym not in DEFAULT_WATCHLIST and sym not in st.session_state.extra_symbols:
        st.session_state.extra_symbols.append(sym)


def active_symbols():
    """自选清单 + 你搜索过的股票，去重后合并，供异动雷达/风险事件/关注清单使用。"""
    return DEFAULT_WATCHLIST + [s for s in st.session_state.extra_symbols if s not in DEFAULT_WATCHLIST]


# ---------------------------------------------------------------------------
# 主题配色
# ---------------------------------------------------------------------------

theme = st.session_state.theme
if theme == "深色":
    bg, surface, surface2, border, text, text_dim = "#080c15", "#101725", "#0c111d", "#232d42", "#eef3ff", "#8995ad"
    up, down, yellow, blue = "#37d39b", "#ff687e", "#f3c85b", "#62a0ff"
    template = "plotly_dark"
    vol_dim = "#2b3549"
else:
    bg, surface, surface2, border, text, text_dim = "#f5f7fb", "#ffffff", "#eef1f8", "#dde3ee", "#141a26", "#66708a"
    up, down, yellow, blue = "#1f9d67", "#e0393f", "#b8790a", "#2f6fe0"
    template = "plotly_white"
    vol_dim = "#d7dce6"

st.markdown(f"""
<style>
.stApp {{ background-color:{bg}; color:{text}; }}
div[data-testid="stMetricValue"] {{ color:{text}; }}
section[data-testid="stSidebar"] {{ background-color:{surface2}; border-right:1px solid {border}; }}
div[data-testid="stVerticalBlockBorderWrapper"] {{ border-color:{border} !important; }}
</style>
""", unsafe_allow_html=True)

if not TWELVE_KEY:
    st.error("还没设置 Twelve Data 的 API Key。去 App 的 Settings → Secrets 加一行：`TWELVE_DATA_API_KEY = \"你的key\"`。")
    st.stop()

# ---------------------------------------------------------------------------
# 侧边栏：Logo + 分页导航 + 主题
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(f"<div style='font-size:24px;font-weight:800;padding:6px 0 18px'>Stock<span style='color:{blue}'>AI</span></div>", unsafe_allow_html=True)

    nav_items = [
        ("overview", "◈ 总览"),
        ("anomaly", "⚡ 异动雷达"),
        ("events", "◷ 风险事件"),
        ("watchlist", "★ AI关注清单"),
    ]
    for key, label in nav_items:
        is_active = st.session_state.page == key or (key == "overview" and st.session_state.page == "stock" and False)
        if st.button(label, use_container_width=True, key=f"nav_{key}",
                     type="primary" if st.session_state.page == key else "secondary"):
            st.session_state.page = key

    st.markdown("---")
    st.session_state.theme = st.radio("主题", ["深色", "浅色"], horizontal=True, index=0 if theme == "深色" else 1, label_visibility="collapsed")

# ---------------------------------------------------------------------------
# 顶部：常驻搜索栏 + 快捷清单（不随分页收起）
# ---------------------------------------------------------------------------

sc1, sc2 = st.columns([5, 1])
search_input = sc1.text_input("搜索", placeholder="🔍 输入任意美股代码查询，如 GOOGL、AMD、PLTR",
                               label_visibility="collapsed")
search_go = sc2.button("查询", use_container_width=True, type="primary")
if search_go and search_input.strip():
    goto_stock(search_input.strip().upper())

wcols = st.columns(len(DEFAULT_WATCHLIST))
for i, sym in enumerate(DEFAULT_WATCHLIST):
    if wcols[i].button(sym, use_container_width=True, key=f"wl_{sym}"):
        goto_stock(sym)

st.markdown("---")

# ---------------------------------------------------------------------------
# 通用：拉取自选清单概况（只用quote，不用time_series，省额度）
# ---------------------------------------------------------------------------

def load_watchlist_overview():
    out = {}
    for s in active_symbols():
        try:
            q = get_quote(s)
            ratio, _ = vol_ratio_from_quote(q)
            pct = float(q.get("percent_change") or 0)
            out[s] = {
                "name": q.get("name", s), "price": float(q.get("close") or 0),
                "pct": pct, "ratio": ratio, "heavy": ratio >= 1.15, "early": ratio >= 1.5,
                "news_count": len(get_news(s)), "score": anomaly_score(pct, ratio),
                "sig": signal_score(pct, ratio),
            }
        except Exception:
            continue
    return out


def load_risk_items():
    items = [(s, get_earnings_date(s)) for s in active_symbols()]
    items = [(s, d) for s, d in items if d]
    items.sort(key=lambda x: x[1])
    return items


def anomaly_card(s, d, key_prefix):
    with st.container(border=True):
        badge = "🔴 提前预警" if d["early"] else ("🟡 关注" if d["heavy"] else "")
        if badge:
            st.markdown(f"<span style='font-size:11px;color:{yellow};font-weight:600'>{badge}</span>", unsafe_allow_html=True)
        st.markdown(f"**{s}** <span style='color:{text_dim};font-size:11px'>{d['name']}</span>", unsafe_allow_html=True)
        color = up if d["pct"] >= 0 else down
        st.markdown(f"<span style='color:{color};font-family:monospace;font-size:22px;font-weight:700'>{d['pct']:+.2f}%</span>", unsafe_allow_html=True)
        vol_tag = "🟠放量" if d["heavy"] else "⚪缩量"
        st.caption(f"成交量 {vol_tag} {d['ratio']:.1f}× · 📰{d['news_count']}条新闻")
        if st.button("查看详情", key=f"{key_prefix}_{s}", use_container_width=True):
            goto_stock(s)


def event_card(s, date_str, key_prefix):
    with st.container(border=True):
        st.markdown(f"**📊 {s}**")
        st.caption(f"预计财报：{date_str}")
        if st.button("查看", key=f"{key_prefix}_{s}", use_container_width=True):
            goto_stock(s)


def watchlist_card(s, d, key_prefix):
    with st.container(border=True):
        c1, c2 = st.columns([2, 1])
        with c1:
            st.markdown(f"**{s}** <span style='color:{text_dim};font-size:11px'>{d['name']}</span>", unsafe_allow_html=True)
            color = up if d["pct"] >= 0 else down
            st.markdown(f"<span style='color:{color};font-family:monospace;font-size:18px;font-weight:700'>{d['pct']:+.2f}%</span>", unsafe_allow_html=True)
        with c2:
            sig_color = up if d["sig"] >= 60 else (down if d["sig"] <= 40 else yellow)
            st.markdown(f"<div style='text-align:right'><span style='font-size:26px;font-weight:800;color:{sig_color}'>{d['sig']}</span><span style='color:{text_dim};font-size:12px'>/100</span></div>", unsafe_allow_html=True)
        st.caption("综合信号分（非AI，基于涨跌幅+成交量计算）· " + ("看多倾向" if d["sig"] >= 60 else "风险偏高" if d["sig"] <= 40 else "中性"))
        if st.button("查看AI分析", key=f"{key_prefix}_{s}", use_container_width=True):
            goto_stock(s)


# ---------------------------------------------------------------------------
# 页面：总览
# ---------------------------------------------------------------------------

def render_overview():
    st.markdown("### 早安 👋")
    st.caption("AI 会告诉你发生了什么、为什么、接下来要关注什么——而不只是丢一堆数字给你。")

    wl = load_watchlist_overview()

    st.markdown("##### 🧠 AI Market Brief")
    if wl:
        n_up = sum(1 for d in wl.values() if d["pct"] >= 0)
        n_down = len(wl) - n_up
        heavy_syms = [s for s, d in wl.items() if d["heavy"]]
        with st.container(border=True):
            st.write(f"自选清单中，今日 **{n_up}** 支上涨、**{n_down}** 支下跌。" +
                     (f"成交量明显放大的有：**{', '.join(heavy_syms)}**，值得多留意。" if heavy_syms else "目前没有股票出现明显的成交量异常。"))
        st.caption("以上为基于当前数据的自动摘要，非AI生成的深度解读；如需AI深度分析，进入具体股票详情页查看。")
    else:
        st.caption("暂无数据")

    st.write("")
    st.markdown("##### ⚡ 异动雷达 · 提前发现")
    ranked = sorted(wl.items(), key=lambda kv: kv[1]["score"], reverse=True)[:3] if wl else []
    if ranked:
        cols = st.columns(len(ranked))
        for i, (s, d) in enumerate(ranked):
            with cols[i]:
                anomaly_card(s, d, "ov_an")
        if st.button("查看全部异动 →", key="ov_see_anomaly"):
            st.session_state.page = "anomaly"
    else:
        st.caption("暂无数据（可能刚触发限速，请稍候）")

    st.write("")
    st.markdown("##### ◷ 近期风险事件")
    risk_items = load_risk_items()[:3]
    if risk_items:
        cols = st.columns(len(risk_items))
        for i, (s, d) in enumerate(risk_items):
            with cols[i]:
                event_card(s, d, "ov_ev")
        if st.button("查看完整日历 →", key="ov_see_events"):
            st.session_state.page = "events"
    else:
        st.caption("暂无已知财报日期")

    st.write("")
    st.markdown("##### ★ AI关注清单")
    top_watch = sorted(wl.items(), key=lambda kv: kv[1]["sig"], reverse=True)[:3] if wl else []
    if top_watch:
        cols = st.columns(len(top_watch))
        for i, (s, d) in enumerate(top_watch):
            with cols[i]:
                watchlist_card(s, d, "ov_wl")
        if st.button("查看完整清单 →", key="ov_see_watchlist"):
            st.session_state.page = "watchlist"


# ---------------------------------------------------------------------------
# 页面：异动雷达
# ---------------------------------------------------------------------------

def render_anomaly():
    st.markdown("### ⚡ 异动雷达")
    st.caption("提前发现突然上涨/下跌、成交量异常的股票——这是本系统最核心的功能。")
    wl = load_watchlist_overview()
    ranked = sorted(wl.items(), key=lambda kv: kv[1]["score"], reverse=True)
    if not ranked:
        st.caption("暂无数据（可能刚触发限速，请稍候）")
        return
    for i in range(0, len(ranked), 3):
        row = ranked[i:i + 3]
        cols = st.columns(3)
        for j, (s, d) in enumerate(row):
            with cols[j]:
                anomaly_card(s, d, "an_full")


# ---------------------------------------------------------------------------
# 页面：风险事件
# ---------------------------------------------------------------------------

def render_events():
    st.markdown("### ◷ 风险事件日历")
    st.caption("自动汇总自选股即将到来的财报等事件，不用一支一支去查。")
    risk_items = load_risk_items()
    if not risk_items:
        st.caption("暂无已知财报日期（yfinance 数据有限，仅供参考）")
        return
    for i in range(0, len(risk_items), 3):
        row = risk_items[i:i + 3]
        cols = st.columns(3)
        for j, (s, d) in enumerate(row):
            with cols[j]:
                event_card(s, d, "ev_full")


# ---------------------------------------------------------------------------
# 页面：AI关注清单
# ---------------------------------------------------------------------------

def render_watchlist():
    st.markdown("### ★ AI关注清单")
    st.caption("综合涨跌幅与成交量算出的信号分，供参考——不是买卖建议。")
    wl = load_watchlist_overview()
    ranked = sorted(wl.items(), key=lambda kv: kv[1]["sig"], reverse=True)
    if not ranked:
        st.caption("暂无数据")
        return
    for i in range(0, len(ranked), 3):
        row = ranked[i:i + 3]
        cols = st.columns(3)
        for j, (s, d) in enumerate(row):
            with cols[j]:
                watchlist_card(s, d, "wl_full")


# ---------------------------------------------------------------------------
# 页面：股票详情（AI Summary → 原因 → 异动 → 新闻 → 风险事件 → K线）
# ---------------------------------------------------------------------------

def render_stock_detail(symbol):
    try:
        quote = get_quote(symbol)
    except Exception as e:
        st.error(f"⚠ 查询「{symbol}」失败：{e}")
        st.caption("常见原因：代码打错了、达到免费额度限速（等一分钟）、或该股票不在Twelve Data覆盖范围。")
        return

    name = quote.get("name", symbol)
    try:
        price = float(quote.get("close") or 0)
    except Exception:
        price = 0.0
    try:
        change_pct = float(quote.get("percent_change") or 0)
    except Exception:
        change_pct = 0.0

    ratio, avg_volume = vol_ratio_from_quote(quote)
    heavy = ratio >= 1.15
    news_items = get_news(symbol)
    earn_date = get_earnings_date(symbol)

    if st.button("← 返回", key="back_btn"):
        st.session_state.page = "overview"
        st.rerun()

    h1, h2, h3 = st.columns([2, 1, 1.3])
    with h1:
        st.markdown(f"### {symbol} · {name}")
    with h2:
        st.metric("现价", f"${price:.2f}", f"{change_pct:+.2f}%")
    with h3:
        if change_pct >= 0:
            sig = "🟢放量上涨·确认" if heavy else "🟡缩量上涨·谨慎"
        else:
            sig = "🔴放量下跌·抛压真实" if heavy else "🟡缩量下跌·或有反弹"
        st.metric("量价信号", sig)

    tab_ai, tab_anomaly, tab_news, tab_events, tab_chart = st.tabs(
        ["🤖 AI Stock Brief", "⚠️ 异动检测", f"📰 新闻 ({len(news_items)})", "📅 风险事件", "📉 K线图"]
    )

    # ---- Tab 1: AI Stock Brief ----
    with tab_ai:
        if not GEMINI_KEY:
            st.info("还没设置 Gemini API Key，AI分析暂时关闭（不影响其他功能）。")
        else:
            brief = get_ai_brief(symbol, name, price, change_pct, ratio, heavy,
                                  [n["title"] for n in news_items], earn_date)
            if brief is None:
                st.caption("AI摘要生成失败")
            elif "error" in brief:
                st.caption(f"AI摘要暂时无法生成：{brief['error']}")
            else:
                with st.container(border=True):
                    st.markdown(f"**{brief.get('summary', '')}**")
                st.markdown("###### 💡 可能原因")
                with st.container(border=True):
                    st.write(brief.get("reason", ""))
                st.markdown("###### 👀 值得关注")
                watch = brief.get("watch", [])
                with st.container(border=True):
                    if watch:
                        for w in watch:
                            st.markdown(f"- {w}")
                    else:
                        st.caption("暂无")
                st.caption("以上内容由AI基于当前数据生成，可能不完整或有误，不构成投资建议。")

    # ---- Tab 2: 异动检测 ----
    with tab_anomaly:
        if ratio >= 1.5:
            st.warning(f"🔴 提前异动信号：最近成交量为均量 **{ratio:.1f}倍**，明显放大，可能早于消息面公开就已经有资金在动作。")
        elif heavy:
            st.info(f"🟡 成交量略高于日均（{ratio:.1f}倍），可关注是否持续放大。")
        else:
            st.caption("目前没有检测到明显的成交量异常。")

    # ---- Tab 3: 新闻 ----
    with tab_news:
        if not news_items:
            st.caption("暂无相关新闻")
        else:
            for n in news_items:
                with st.container(border=True):
                    if n["url"]:
                        st.markdown(f"**[{n['title']}]({n['url']})**")
                    else:
                        st.markdown(f"**{n['title']}**")
                    st.caption(f"{n['src']} · {n['time']}")

    # ---- Tab 4: 风险事件 ----
    with tab_events:
        if earn_date:
            st.info(f"预计财报日期：{earn_date}")
        else:
            st.caption("暂无已知财报日期")

    # ---- Tab 5: K线（辅助，勾选后才加载，省API额度）----
    with tab_chart:
        range_label = st.selectbox("显示范围", ["今天", "最近3天", "最近5天"], index=0,
                                    help="选\"今天\"时画线最准确；跨天会因为隐藏了收盘到开盘之间的空档，斜线在空档两侧会看起来不连贯，这是正常现象。")
        range_map = {"今天": 14, "最近3天": 42, "最近5天": 70}
        load_chart = st.checkbox("加载并显示K线图", value=False, key="load_chart_toggle")

        if load_chart:
            try:
                df = get_time_series(symbol, interval="30min", outputsize=range_map[range_label])
            except Exception as e:
                df = None
                st.error(f"K线数据获取失败：{e}")

            if df is not None:
                ctl1, ctl2, ctl3 = st.columns([1.3, 1, 1])
                chart_type = ctl1.radio("图表类型", ["K线图", "趋势图"], horizontal=True, label_visibility="collapsed")
                show_sma = ctl2.checkbox("均线 SMA5")
                show_sr = ctl3.checkbox("阻力/支撑")

                # ---- 量价真假判断（成交量能否验证价格走势，含"是否可能是空头行为"的定性说明）----
                verdict_up = change_pct >= 0
                if heavy:
                    if verdict_up:
                        vp_text = "🟢 **放量上涨** — 成交量明显放大，买盘意愿真实，属于量价配合的健康上涨；但无法仅凭价量数据100%确认是否有做空回补参与，需结合更多信息判断。"
                    else:
                        vp_text = "🔴 **放量下跌** — 成交量明显放大，说明抛压是真实的，不像是无量阴跌；这种情况下也更可能包含新增做空动作，但价量数据本身无法直接证实是否有人做空（缺少融券/空头持仓数据）。"
                else:
                    if verdict_up:
                        vp_text = "🟡 **缩量上涨** — 成交量没有明显放大，上涨动能可能不足，需警惕是空头回补造成的短线反弹，而非真实新增买盘，追高需谨慎。"
                    else:
                        vp_text = "🟡 **缩量下跌** — 成交量没有明显放大，可能只是情绪性抛售或获利了结，不像是大规模出货，后续存在修复可能。"
                with st.container(border=True):
                    st.markdown(vp_text)
                    st.caption("说明：免费数据源不含融券/空头持仓数据，以上仅根据价格与成交量的搭配关系做定性推测，不是对是否做空的确认。")

                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])

                if chart_type == "K线图":
                    fig.add_trace(go.Candlestick(
                        x=df["datetime"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                        increasing_line_color=up, decreasing_line_color=down, name="K线"
                    ), row=1, col=1)
                else:
                    is_up = df["close"].iloc[-1] >= df["close"].iloc[0]
                    clr = up if is_up else down
                    fig.add_trace(go.Scatter(
                        x=df["datetime"], y=df["close"], mode="lines", name="价格",
                        line=dict(width=2, color=clr), fill="tozeroy",
                        fillcolor="rgba(51,214,159,0.08)" if is_up else "rgba(255,103,103,0.08)"
                    ), row=1, col=1)

                if show_sma:
                    sma = compute_sma(df["close"], 5)
                    fig.add_trace(go.Scatter(x=df["datetime"], y=sma, mode="lines", name="SMA5",
                                              line=dict(dash="dot", color=yellow)), row=1, col=1)

                if show_sr:
                    resistances, supports = compute_support_resistance(df)
                    for r in resistances:
                        fig.add_hline(y=r, line_dash="dash", line_color=down, row=1, col=1,
                                      annotation_text=f"阻力 ${r:.2f}", annotation_position="top left")
                    for s in supports:
                        fig.add_hline(y=s, line_dash="dash", line_color=up, row=1, col=1,
                                      annotation_text=f"支撑 ${s:.2f}", annotation_position="bottom left")

                vol_colors = [(yellow if (v / avg_volume if avg_volume else 0) >= 1.3 else vol_dim) for v in df["volume"]]
                fig.add_trace(go.Bar(x=df["datetime"], y=df["volume"], marker_color=vol_colors, name="成交量"), row=2, col=1)
                if avg_volume:
                    fig.add_hline(y=avg_volume, line_dash="dash", line_color=text_dim, row=2, col=1, annotation_text="均量")

                # 新闻标记：附带 customdata 记录在 news_items 里的下标，点击后可联动显示
                mk_x, mk_y, mk_text, mk_idx = [], [], [], []
                for ni, n in enumerate(news_items):
                    idx = nearest_bar_index(n["time"], df["datetime"])
                    if idx is not None:
                        mk_x.append(df["datetime"].iloc[idx])
                        ref_price = df["high"].iloc[idx] if chart_type == "K线图" else df["close"].iloc[idx]
                        mk_y.append(ref_price * 1.012)
                        mk_text.append(n["title"])
                        mk_idx.append(ni)
                if mk_x:
                    fig.add_trace(go.Scatter(
                        x=mk_x, y=mk_y, mode="markers", name="📰新闻",
                        marker=dict(symbol="diamond", size=12, color=yellow, line=dict(width=1, color=bg)),
                        text=mk_text, customdata=mk_idx, hovertemplate="📰 %{text}<extra></extra>",
                    ), row=1, col=1)

                # 当前价格标签（贴在价格轴右边，模拟"现价跳动"的即时价签；每次页面刷新会更新到最新值）
                fig.add_annotation(
                    xref="paper", x=1.0, y=df["close"].iloc[-1], yref="y",
                    text=f" ${df['close'].iloc[-1]:.2f} ", showarrow=False,
                    font=dict(color=bg, size=12, family="monospace"),
                    bgcolor=(up if df["close"].iloc[-1] >= df["open"].iloc[0] else down),
                    bordercolor=(up if df["close"].iloc[-1] >= df["open"].iloc[0] else down),
                    borderwidth=1, borderpad=3, xanchor="left", row=1, col=1,
                )

                fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[16, 9.5], pattern="hour")])
                fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
                fig.update_xaxes(rangeslider_visible=True, rangeslider_thickness=0.06, row=2, col=1)
                fig.update_yaxes(title_text="价格 ($)", side="right", row=1, col=1)
                fig.update_yaxes(title_text="成交量", side="right", row=2, col=1)
                fig.update_layout(
                    template=template, height=520, margin=dict(l=10, r=60, t=40, b=10),
                    showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                    dragmode="pan", paper_bgcolor=surface, plot_bgcolor=surface,
                )
                click_data = st.plotly_chart(
                    fig, use_container_width=True,
                    config={
                        "scrollZoom": True, "displaylogo": False,
                        "modeBarButtonsToAdd": ["drawline", "eraseshape"],
                    },
                    on_select="rerun", selection_mode=("points",), key="price_chart",
                )
                st.caption("滚轮/中间拖动=整体缩放平移；鼠标放最右边价格数字上下拖=单独缩放价格轴。工具栏「✏️画线」可画趋势线，画完点这条线再点「🗑️」删除。🟡菱形=新闻，点一下在下面显示对应新闻。")

                # 点击图上的新闻菱形标记，联动显示对应新闻内容
                try:
                    pts = click_data["selection"]["points"]
                except Exception:
                    pts = []
                clicked_news_idx = None
                for p in pts:
                    cd = p.get("customdata")
                    if cd is not None:
                        clicked_news_idx = cd if isinstance(cd, int) else (cd[0] if isinstance(cd, (list, tuple)) else None)
                        break
                if clicked_news_idx is not None and 0 <= clicked_news_idx < len(news_items):
                    n = news_items[clicked_news_idx]
                    with st.container(border=True):
                        st.markdown(f"📰 **[{n['title']}]({n['url']})**" if n["url"] else f"📰 **{n['title']}**")
                        st.caption(f"{n['src']} · {n['time']}")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

page = st.session_state.page
if page == "overview":
    render_overview()
elif page == "anomaly":
    render_anomaly()
elif page == "events":
    render_events()
elif page == "watchlist":
    render_watchlist()
else:
    render_stock_detail(st.session_state.current_symbol)

st.markdown("---")
st.caption("行情来自 Twelve Data（可能有延迟），新闻来自 Yahoo Finance，AI分析来自 Google Gemini。仅供个人参考，不构成投资建议。")
