"""
盘中异动 · 美股新闻联动看板（Streamlit 版）

数据来源：
- 行情/K线/成交量：Twelve Data（免费额度：每天800次，每分钟8次；免费版不需要绑卡，超额度只会报错，不会自动扣费）
- 新闻/财报日期：yfinance（完全免费，不需要注册和key）

省额度设计：
- 所有API调用都做了缓存（st.cache_data），同一支股票短时间内重复查看不会重复消耗额度
- 只有你自己在用的情况下，几乎不可能碰到免费额度上限
"""

import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf

st.set_page_config(page_title="盘中异动 · 美股新闻联动看板", layout="wide", page_icon="📊")

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META"]

TWELVE_KEY = st.secrets.get("TWELVE_DATA_API_KEY", "")

# ---------------------------------------------------------------------------
# 数据获取（带缓存，保护免费额度）
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_quote(symbol: str):
    r = requests.get(
        "https://api.twelvedata.com/quote",
        params={"symbol": symbol, "apikey": TWELVE_KEY},
        timeout=15,
    )
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "查询失败，股票代码可能不存在"))
    return data


@st.cache_data(ttl=300, show_spinner=False)
def get_time_series(symbol: str, interval: str = "30min", outputsize: int = 30):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVE_KEY},
        timeout=15,
    )
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
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


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
        if isinstance(content.get("provider"), dict):
            src = content["provider"].get("displayName", "")
        else:
            src = item.get("publisher", "")
        if isinstance(content.get("canonicalUrl"), dict):
            url = content["canonicalUrl"].get("url", "")
        else:
            url = item.get("link", "")
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
# 技术分析（纯数学计算，不额外消耗API额度）
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
    resistances = sorted(set(maxima), reverse=True)[:n]
    supports = sorted(set(minima))[:n]
    return resistances, supports


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("📊 控制面板")

    theme = st.radio("背景主题", ["深色", "浅色"], horizontal=True)

    st.markdown("---")
    st.subheader("自选股快捷选择")
    cols = st.columns(2)
    picked = None
    for i, sym in enumerate(DEFAULT_WATCHLIST):
        if cols[i % 2].button(sym, use_container_width=True, key=f"quick_{sym}"):
            picked = sym

    st.markdown("---")
    st.subheader("🔍 查任意股票")
    search_input = st.text_input("输入股票代码（如 GOOGL、AMD、PLTR）", "").strip().upper()
    search_go = st.button("查询", use_container_width=True, type="primary")

    st.markdown("---")
    st.subheader("技术分析线")
    show_sma = st.checkbox("显示均线（SMA5）")
    show_sr = st.checkbox("显示阻力 / 支撑线")

if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = DEFAULT_WATCHLIST[0]
if picked:
    st.session_state.current_symbol = picked
if search_go and search_input:
    st.session_state.current_symbol = search_input

symbol = st.session_state.current_symbol

# ---------------------------------------------------------------------------
# 主题样式
# ---------------------------------------------------------------------------

if theme == "深色":
    bg, surface, text, text_dim, template = "#10151c", "#171e27", "#e7ecf2", "#7c8798", "plotly_dark"
else:
    bg, surface, text, text_dim, template = "#ffffff", "#f5f6f8", "#1a1f26", "#6b7280", "plotly_white"

st.markdown(
    f"""
    <style>
    .stApp {{ background-color:{bg}; color:{text}; }}
    [data-testid="stMetricValue"] {{ color:{text}; }}
    [data-testid="stSidebar"] {{ background-color:{surface}; }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("盘中异动 · 美股新闻联动看板")
st.caption("行情来自 Twelve Data（可能有延迟），新闻来自 Yahoo Finance，随查随有。仅供个人参考，不构成投资建议。")

if not TWELVE_KEY:
    st.error("还没设置 Twelve Data 的 API Key。去这个 App 的 Settings → Secrets 里加一行：`TWELVE_DATA_API_KEY = \"你的key\"`，保存后会自动重启生效。")
    st.stop()

# ---------------------------------------------------------------------------
# 抓数据
# ---------------------------------------------------------------------------

try:
    quote = get_quote(symbol)
    df = get_time_series(symbol, interval="30min", outputsize=30)
except Exception as e:
    st.error(f"⚠ 查询 「{symbol}」 失败：{e}")
    st.caption("常见原因：股票代码打错了、当前已达免费额度上限（等一分钟再试）、或者该股票不在Twelve Data覆盖范围内。")
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

avg_volume = df["volume"].mean() if len(df) else 0
latest_volume = df["volume"].iloc[-1] if len(df) else 0
latest_ratio = (latest_volume / avg_volume) if avg_volume else 1

col1, col2, col3 = st.columns([2.2, 1, 1.2])
with col1:
    st.subheader(f"{symbol} · {name}")
with col2:
    st.metric("现价", f"${price:.2f}", f"{change_pct:+.2f}%")
with col3:
    heavy = latest_ratio >= 1.15
    if change_pct >= 0:
        sig = "🟢 放量上涨 · 确认" if heavy else "🟡 缩量上涨 · 追高谨慎"
    else:
        sig = "🔴 放量下跌 · 抛压真实" if heavy else "🟡 缩量下跌 · 或有反弹"
    st.metric("量价信号", sig)

if avg_volume and latest_ratio >= 1.5:
    st.warning(f"⚠ 早期异动信号：最近一段成交量为均量的 **{latest_ratio:.1f} 倍**，明显放大，值得留意是否有消息面变化。")

# ---------------------------------------------------------------------------
# 价格图
# ---------------------------------------------------------------------------

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=df["datetime"], y=df["close"], mode="lines", name="价格",
    line=dict(width=2, color="#33d69f" if change_pct >= 0 else "#ff6767"),
    fill="tozeroy", fillcolor="rgba(51,214,159,0.08)" if change_pct >= 0 else "rgba(255,103,103,0.08)",
))

if show_sma:
    sma = compute_sma(df["close"], window=5)
    fig.add_trace(go.Scatter(x=df["datetime"], y=sma, mode="lines", name="SMA5", line=dict(dash="dot", color="#f2b84b")))

if show_sr:
    resistances, supports = compute_support_resistance(df)
    for r in resistances:
        fig.add_hline(y=r, line_dash="dash", line_color="#ff6767", annotation_text=f"阻力 ${r:.2f}", annotation_position="top left")
    for s in supports:
        fig.add_hline(y=s, line_dash="dash", line_color="#33d69f", annotation_text=f"支撑 ${s:.2f}", annotation_position="bottom left")

fig.update_layout(
    template=template, height=380, margin=dict(l=10, r=10, t=20, b=10),
    yaxis_title="价格 ($)", showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig, use_container_width=True)

# 成交量图
vol_colors = ["#f2b84b" if (v / avg_volume if avg_volume else 0) >= 1.3 else ("#3a4657" if theme == "深色" else "#c7ccd4") for v in df["volume"]]
vol_fig = go.Figure()
vol_fig.add_trace(go.Bar(x=df["datetime"], y=df["volume"], marker_color=vol_colors, name="成交量"))
if avg_volume:
    vol_fig.add_hline(y=avg_volume, line_dash="dash", line_color=text_dim, annotation_text="当日均量")
vol_fig.update_layout(template=template, height=150, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="成交量")
st.plotly_chart(vol_fig, use_container_width=True)

# ---------------------------------------------------------------------------
# 新闻
# ---------------------------------------------------------------------------

st.subheader("📰 相关新闻")
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

# ---------------------------------------------------------------------------
# 财报日期
# ---------------------------------------------------------------------------

st.subheader("📅 财报日期")
earn_date = get_earnings_date(symbol)
if earn_date:
    st.info(f"预计财报日期：{earn_date}")
else:
    st.caption("暂无已知财报日期信息")

st.markdown("---")
st.caption("行情数据可能有延迟；新闻与价格按时间就近展示，不代表确定的因果关系，请自行判断。仅作个人参考，不构成投资建议。")
