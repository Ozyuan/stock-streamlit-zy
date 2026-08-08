"""
盘中异动 · 美股新闻联动看板（Streamlit 版 v2）
K线图 + 趋势图切换、可缩放拖动、画线工具、今日异动、风险日历、新闻侧栏
数据来源：Twelve Data（行情/K线/成交量，免费额度每天800次）+ yfinance（新闻/财报，免费无需key）
"""

import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf

st.set_page_config(page_title="盘中异动 · 美股新闻联动看板", layout="wide", page_icon="📊")

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META"]
TWELVE_KEY = st.secrets.get("TWELVE_DATA_API_KEY", "")

# ---------------------------------------------------------------------------
# 数据获取（带缓存，保护免费额度）
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
# 技术分析
# ---------------------------------------------------------------------------

def compute_sma(series: pd.Series, window: int = 5):
    return series.rolling(window=window, min_periods=1).mean()


def compute_support_resistance(df: pd.DataFrame, n: int = 2):
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
    return sorted(set(maxima), reverse=True)[:n], sorted(set(minima))[:n]


def vol_ratio_of(df):
    avg = df["volume"].mean() if len(df) else 0
    latest = df["volume"].iloc[-1] if len(df) else 0
    return (latest / avg) if avg else 1, avg


# ---------------------------------------------------------------------------
# 状态初始化
# ---------------------------------------------------------------------------

if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = DEFAULT_WATCHLIST[0]
if "theme" not in st.session_state:
    st.session_state.theme = "深色"

# ---------------------------------------------------------------------------
# 顶部：标题 + 主题切换 + 搜索栏 + 快捷清单（常驻，不做成可收起的侧边栏）
# ---------------------------------------------------------------------------

top_l, top_r = st.columns([4, 1])
with top_l:
    st.markdown("### 📊 盘中异动 · 美股新闻联动看板")
with top_r:
    st.session_state.theme = st.radio("主题", ["深色", "浅色"], horizontal=True,
                                       label_visibility="collapsed",
                                       index=0 if st.session_state.theme == "深色" else 1)

sc1, sc2 = st.columns([5, 1])
search_input = sc1.text_input("搜索", placeholder="🔍 输入任意美股代码查询，如 GOOGL、AMD、PLTR",
                               label_visibility="collapsed")
search_go = sc2.button("查询", use_container_width=True, type="primary")
if search_go and search_input.strip():
    st.session_state.current_symbol = search_input.strip().upper()

wcols = st.columns(len(DEFAULT_WATCHLIST))
for i, sym in enumerate(DEFAULT_WATCHLIST):
    if wcols[i].button(sym, use_container_width=True, key=f"wl_{sym}"):
        st.session_state.current_symbol = sym

theme = st.session_state.theme
if theme == "深色":
    bg, surface, text, text_dim, template = "#10151c", "#171e27", "#e7ecf2", "#7c8798", "plotly_dark"
    vol_dim_color = "#3a4657"
else:
    bg, surface, text, text_dim, template = "#ffffff", "#f5f6f8", "#1a1f26", "#6b7280", "plotly_white"
    vol_dim_color = "#c7ccd4"

st.markdown(f"""
<style>
.stApp {{ background-color:{bg}; color:{text}; }}
div[data-testid="stMetricValue"] {{ color:{text}; }}
</style>
""", unsafe_allow_html=True)

if not TWELVE_KEY:
    st.error("还没设置 Twelve Data 的 API Key。去 App 的 Settings → Secrets 加一行：`TWELVE_DATA_API_KEY = \"你的key\"`。")
    st.stop()

st.markdown("---")

# ---------------------------------------------------------------------------
# 今日异动 TOP（跟专业软件不同：把"相关新闻数量+量价信号"直接摆出来，不用切页面对照）
# ---------------------------------------------------------------------------

st.markdown("##### 🔥 今日异动 TOP · 新闻与量价信号一目了然")
mv_data = {}
for s in DEFAULT_WATCHLIST:
    try:
        q = get_quote(s)
        ts = get_time_series(s, interval="30min", outputsize=30)
        ratio, _ = vol_ratio_of(ts)
        mv_data[s] = {
            "name": q.get("name", s),
            "pct": float(q.get("percent_change") or 0),
            "heavy": ratio >= 1.15,
            "ratio": ratio,
            "news_count": len(get_news(s)),
        }
    except Exception:
        continue

ranked = sorted(mv_data.items(), key=lambda kv: abs(kv[1]["pct"]), reverse=True)[:4]
if ranked:
    mcols = st.columns(len(ranked))
    for i, (s, d) in enumerate(ranked):
        with mcols[i]:
            with st.container(border=True):
                st.markdown(f"**{s}**  <span style='color:{text_dim};font-size:11px'>{d['name']}</span>", unsafe_allow_html=True)
                color = "#33d69f" if d["pct"] >= 0 else "#ff6767"
                st.markdown(f"<span style='color:{color};font-family:monospace;font-size:20px;font-weight:600'>{d['pct']:+.2f}%</span>", unsafe_allow_html=True)
                vol_tag = "🟠放量" if d["heavy"] else "⚪缩量"
                st.caption(f"📰 {d['news_count']}条新闻 · {vol_tag} {d['ratio']:.1f}×")
                if st.button("查看", key=f"mv_{s}", use_container_width=True):
                    st.session_state.current_symbol = s
else:
    st.caption("暂无数据（可能刚触发限速，请稍候）")

st.write("")

# ---------------------------------------------------------------------------
# 风险日历（汇总自选股财报日期）
# ---------------------------------------------------------------------------

st.markdown("##### 📅 近期风险事件（财报日期）")
risk_items = []
for s in DEFAULT_WATCHLIST:
    d = get_earnings_date(s)
    if d:
        risk_items.append((s, d))
risk_items.sort(key=lambda x: x[1])
if risk_items:
    rcols = st.columns(len(risk_items))
    for i, (s, d) in enumerate(risk_items):
        with rcols[i]:
            with st.container(border=True):
                st.markdown(f"**📊 {s}**")
                st.caption(f"预计财报：{d}")
else:
    st.caption("暂无已知财报日期（yfinance 数据有限，仅供参考）")

st.markdown("---")

# ---------------------------------------------------------------------------
# 主区域：左边图表，右边新闻（新闻常驻侧边，不用往下拉）
# ---------------------------------------------------------------------------

symbol = st.session_state.current_symbol

try:
    quote = get_quote(symbol)
    df = get_time_series(symbol, interval="30min", outputsize=60)
except Exception as e:
    st.error(f"⚠ 查询「{symbol}」失败：{e}")
    st.caption("常见原因：代码打错了、达到免费额度限速（等一分钟）、或该股票不在Twelve Data覆盖范围。")
    st.stop()

name = quote.get("name", symbol)
try:
    price = float(quote.get("close") or df["close"].iloc[-1])
except Exception:
    price = float(df["close"].iloc[-1])
try:
    change_pct = float(quote.get("percent_change") or 0)
except Exception:
    change_pct = 0.0

ratio, avg_volume = vol_ratio_of(df)

left, right = st.columns([2.3, 1])

with left:
    h1, h2, h3 = st.columns([2, 1, 1.3])
    with h1:
        st.subheader(f"{symbol} · {name}")
    with h2:
        st.metric("现价", f"${price:.2f}", f"{change_pct:+.2f}%")
    with h3:
        heavy = ratio >= 1.15
        if change_pct >= 0:
            sig = "🟢放量上涨·确认" if heavy else "🟡缩量上涨·谨慎"
        else:
            sig = "🔴放量下跌·抛压真实" if heavy else "🟡缩量下跌·或有反弹"
        st.metric("量价信号", sig)

    if avg_volume and ratio >= 1.5:
        st.warning(f"⚠ 早期异动信号：最近一段成交量为均量 **{ratio:.1f}倍**，明显放大。")

    ctl1, ctl2, ctl3 = st.columns([1.3, 1, 1])
    chart_type = ctl1.radio("图表类型", ["K线图", "趋势图"], horizontal=True, label_visibility="collapsed")
    show_sma = ctl2.checkbox("均线 SMA5")
    show_sr = ctl3.checkbox("阻力/支撑")

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])

    if chart_type == "K线图":
        fig.add_trace(go.Candlestick(
            x=df["datetime"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color="#33d69f", decreasing_line_color="#ff6767", name="K线"
        ), row=1, col=1)
    else:
        up = df["close"].iloc[-1] >= df["close"].iloc[0]
        clr = "#33d69f" if up else "#ff6767"
        fig.add_trace(go.Scatter(
            x=df["datetime"], y=df["close"], mode="lines", name="价格",
            line=dict(width=2, color=clr), fill="tozeroy",
            fillcolor="rgba(51,214,159,0.08)" if up else "rgba(255,103,103,0.08)"
        ), row=1, col=1)

    if show_sma:
        sma = compute_sma(df["close"], 5)
        fig.add_trace(go.Scatter(x=df["datetime"], y=sma, mode="lines", name="SMA5",
                                  line=dict(dash="dot", color="#f2b84b")), row=1, col=1)

    if show_sr:
        resistances, supports = compute_support_resistance(df)
        for r in resistances:
            fig.add_hline(y=r, line_dash="dash", line_color="#ff6767", row=1, col=1,
                          annotation_text=f"阻力 ${r:.2f}", annotation_position="top left")
        for s in supports:
            fig.add_hline(y=s, line_dash="dash", line_color="#33d69f", row=1, col=1,
                          annotation_text=f"支撑 ${s:.2f}", annotation_position="bottom left")

    vol_colors = [("#f2b84b" if (v/avg_volume if avg_volume else 0) >= 1.3 else vol_dim_color) for v in df["volume"]]
    fig.add_trace(go.Bar(x=df["datetime"], y=df["volume"], marker_color=vol_colors, name="成交量"), row=2, col=1)
    if avg_volume:
        fig.add_hline(y=avg_volume, line_dash="dash", line_color=text_dim, row=2, col=1, annotation_text="均量")

    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_xaxes(rangeslider_visible=True, rangeslider_thickness=0.06, row=2, col=1)
    fig.update_yaxes(title_text="价格 ($)", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    fig.update_layout(
        template=template, height=560, margin=dict(l=10, r=10, t=10, b=10),
        showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
        dragmode="pan",
    )

    st.plotly_chart(fig, use_container_width=True, config={
        "scrollZoom": True, "displaylogo": False,
        "modeBarButtonsToAdd": ["drawline", "drawopenpath", "drawrect", "drawcircle", "eraseshape"],
    })
    st.caption("提示：滚轮/拖动可缩放平移，右上角工具栏有画线工具（趋势线/矩形/圆形），双击图表可还原。")

with right:
    st.markdown("##### 📰 相关新闻")
    news_items = get_news(symbol)
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

    st.markdown("##### 📅 财报日期")
    earn_date = get_earnings_date(symbol)
    st.info(f"预计：{earn_date}") if earn_date else st.caption("暂无已知财报日期")

st.markdown("---")
st.caption("行情来自 Twelve Data（可能有延迟），新闻来自 Yahoo Finance。仅供个人参考，不构成投资建议。")
