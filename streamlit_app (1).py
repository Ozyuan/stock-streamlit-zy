"""
盘中异动 · AI股票分析看板（Streamlit 版 v3）

核心目标：简单、自动、主动提醒异动、AI解释原因、提前告知风险事件。
K线图退居辅助角色，默认收起。

数据来源：
- 行情/K线/成交量：Twelve Data（免费，每天800次额度，不需要绑卡）
- 新闻/财报日期：yfinance（完全免费，不需要注册）
- AI摘要/原因分析：Google Gemini API（免费额度，Google AI Studio申请，不需要绑卡）
    如果没填 GEMINI_API_KEY，AI相关区块会自动隐藏，其余功能正常使用。
"""

import json
import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf

st.set_page_config(page_title="盘中异动 · AI股票分析看板", layout="wide", page_icon="🤖")

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META"]
TWELVE_KEY = st.secrets.get("TWELVE_DATA_API_KEY", "")
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-lite"

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


def vol_ratio_of(df):
    avg = df["volume"].mean() if len(df) else 0
    latest = df["volume"].iloc[-1] if len(df) else 0
    return (latest / avg) if avg else 1, avg


def anomaly_score(pct, vol_ratio):
    """异动分数：涨跌幅 + 成交量异常加权，用来给"提前发现异动"排序。"""
    return abs(pct) + max(0, vol_ratio - 1) * 4


# ---------------------------------------------------------------------------
# AI Stock Brief（Gemini，免费额度，未设置key时自动跳过）
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def get_ai_brief(symbol, name, price, change_pct, vol_ratio, heavy, news_titles, earn_date):
    if not GEMINI_KEY:
        return None
    vol_desc = f"为均量的 {vol_ratio:.1f} 倍（{'明显放量' if heavy else '量能偏低或持平'}）"
    news_block = "\n".join(f"- {t}" for t in news_titles) if news_titles else "（今天没有抓到相关新闻）"
    prompt = f"""你是一位中立、谨慎的金融数据助理。只根据下面提供的数据做客观解读，不要编造没有依据的信息，不要给出买卖建议，不要做确定性的未来预测（关于未来的表述要用"如果...可能..."这类概率性说法，并明确这只是基于当前数据的推测）。用简体中文回答。

数据：
股票代码：{symbol}（{name}）
现价：${price:.2f}，今日涨跌幅：{change_pct:+.2f}%
成交量：{vol_desc}
今日相关新闻标题：
{news_block}
即将到来的财报日期：{earn_date or '暂无数据'}

请只输出一个JSON对象，不要有任何其他文字、不要markdown代码块符号，格式严格如下：
{{"summary": "两三句话的整体摘要，说清楚今天大致发生了什么", "reason": "结合成交量和新闻分析可能的原因；如果新闻和价格变化关联不明显，要明确说'现有信息不足以确认具体原因，仅供参考'", "watch": ["值得关注的点1（简短一句话）", "值得关注的点2"]}}"""
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=25,
        )
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 状态初始化
# ---------------------------------------------------------------------------

if "current_symbol" not in st.session_state:
    st.session_state.current_symbol = DEFAULT_WATCHLIST[0]
if "theme" not in st.session_state:
    st.session_state.theme = "深色"

# ---------------------------------------------------------------------------
# 顶部：标题 + 主题 + 搜索栏 + 快捷清单（常驻，不做成可收起的侧边栏）
# ---------------------------------------------------------------------------

top_l, top_r = st.columns([4, 1])
with top_l:
    st.markdown("### 🤖 盘中异动 · AI股票分析看板")
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
    bg, surface, text, text_dim, template = "#0d1117", "#161b22", "#e6edf3", "#8b949e", "plotly_dark"
    vol_dim_color, card_border = "#30363d", "#30363d"
else:
    bg, surface, text, text_dim, template = "#ffffff", "#f6f8fa", "#1a1f26", "#6b7280", "plotly_white"
    vol_dim_color, card_border = "#d0d7de", "#d0d7de"

st.markdown(f"""
<style>
.stApp {{ background-color:{bg}; color:{text}; }}
div[data-testid="stMetricValue"] {{ color:{text}; }}
div[data-testid="stExpander"] {{ border-color:{card_border}; }}
</style>
""", unsafe_allow_html=True)

if not TWELVE_KEY:
    st.error("还没设置 Twelve Data 的 API Key。去 App 的 Settings → Secrets 加一行：`TWELVE_DATA_API_KEY = \"你的key\"`。")
    st.stop()

st.markdown("---")

# ---------------------------------------------------------------------------
# ⚠ 异动监控（系统最大特色：提前发现突然上涨/成交量异常/股价异常）
# ---------------------------------------------------------------------------

st.markdown("##### ⚠️ 异动监控 · 提前发现")
wl_data = {}
for s in DEFAULT_WATCHLIST:
    try:
        q = get_quote(s)
        ts = get_time_series(s, interval="30min", outputsize=30)
        ratio, _ = vol_ratio_of(ts)
        pct = float(q.get("percent_change") or 0)
        wl_data[s] = {
            "name": q.get("name", s), "pct": pct, "ratio": ratio,
            "heavy": ratio >= 1.15, "early": ratio >= 1.5,
            "news_count": len(get_news(s)), "score": anomaly_score(pct, ratio),
        }
    except Exception:
        continue

ranked = sorted(wl_data.items(), key=lambda kv: kv[1]["score"], reverse=True)[:4]
if ranked:
    acols = st.columns(len(ranked))
    for i, (s, d) in enumerate(ranked):
        with acols[i]:
            with st.container(border=True):
                badge = "🔴 提前预警" if d["early"] else ("🟡 关注" if d["heavy"] else "")
                if badge:
                    st.markdown(f"<span style='font-size:11px;color:#f2b84b;font-weight:600'>{badge}</span>", unsafe_allow_html=True)
                st.markdown(f"**{s}** <span style='color:{text_dim};font-size:11px'>{d['name']}</span>", unsafe_allow_html=True)
                color = "#33d69f" if d["pct"] >= 0 else "#ff6767"
                st.markdown(f"<span style='color:{color};font-family:monospace;font-size:20px;font-weight:700'>{d['pct']:+.2f}%</span>", unsafe_allow_html=True)
                vol_tag = "🟠放量" if d["heavy"] else "⚪缩量"
                st.caption(f"成交量 {vol_tag} {d['ratio']:.1f}× · 📰{d['news_count']}条新闻")
                if st.button("查看AI分析", key=f"an_{s}", use_container_width=True):
                    st.session_state.current_symbol = s
else:
    st.caption("暂无数据（可能刚触发限速，请稍候）")

st.write("")

# ---------------------------------------------------------------------------
# 📅 近期风险事件
# ---------------------------------------------------------------------------

st.markdown("##### 📅 近期风险事件（财报日期）")
risk_items = [(s, get_earnings_date(s)) for s in DEFAULT_WATCHLIST]
risk_items = [(s, d) for s, d in risk_items if d]
risk_items.sort(key=lambda x: x[1])
if risk_items:
    rcols = st.columns(len(risk_items))
    for i, (s, d) in enumerate(risk_items):
        with rcols[i]:
            with st.container(border=True):
                st.markdown(f"**📊 {s}**")
                st.caption(f"预计财报：{d}")
                if st.button("查看", key=f"rk_{s}", use_container_width=True):
                    st.session_state.current_symbol = s
else:
    st.caption("暂无已知财报日期（yfinance 数据有限，仅供参考）")

st.markdown("---")

# ---------------------------------------------------------------------------
# 股票详情：AI Summary → 原因 → 异动 → 新闻 → 风险事件 → K线（辅助，收起）
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
heavy = ratio >= 1.15
news_items = get_news(symbol)
earn_date = get_earnings_date(symbol)

h1, h2, h3 = st.columns([2, 1, 1.3])
with h1:
    st.subheader(f"{symbol} · {name}")
with h2:
    st.metric("现价", f"${price:.2f}", f"{change_pct:+.2f}%")
with h3:
    if change_pct >= 0:
        sig = "🟢放量上涨·确认" if heavy else "🟡缩量上涨·谨慎"
    else:
        sig = "🔴放量下跌·抛压真实" if heavy else "🟡缩量下跌·或有反弹"
    st.metric("量价信号", sig)

# ---- 1. AI Stock Brief ----
st.markdown("#### 🤖 AI Stock Brief")
if not GEMINI_KEY:
    st.info("还没设置 Gemini API Key，AI分析暂时关闭（不影响其他功能）。去 Google AI Studio 免费申请一个key（不用绑卡），再到本App的 Settings → Secrets 加一行：`GEMINI_API_KEY = \"你的key\"`。")
else:
    brief = get_ai_brief(symbol, name, price, change_pct, ratio, heavy,
                          [n["title"] for n in news_items], earn_date)
    if brief is None:
        st.caption("AI摘要生成失败")
    elif "error" in brief:
        st.caption(f"AI摘要暂时无法生成：{brief['error']}")
    else:
        with st.container(border=True):
            st.markdown(f"**{brief.get('summary','')}**")

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

# ---- 2. 异动检测 ----
st.markdown("#### ⚠️ 异动检测")
if ratio >= 1.5:
    st.warning(f"🔴 提前异动信号：最近一段成交量为均量 **{ratio:.1f}倍**，明显放大，可能早于消息面公开就已经有资金在动作。")
elif heavy:
    st.info(f"🟡 成交量略高于日均（{ratio:.1f}倍），可关注是否持续放大。")
else:
    st.caption("目前没有检测到明显的成交量异常。")

# ---- 3. 新闻 ----
st.markdown("#### 📰 相关新闻")
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

# ---- 4. 风险事件 ----
st.markdown("#### 📅 风险事件")
if earn_date:
    st.info(f"预计财报日期：{earn_date}")
else:
    st.caption("暂无已知财报日期")

# ---- 5. K线（辅助，默认收起）----
with st.expander("📉 K线图（辅助参考，点击展开）", expanded=False):
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

    mk_x, mk_y, mk_text = [], [], []
    for n in news_items:
        idx = nearest_bar_index(n["time"], df["datetime"])
        if idx is not None:
            mk_x.append(df["datetime"].iloc[idx])
            ref_price = df["high"].iloc[idx] if chart_type == "K线图" else df["close"].iloc[idx]
            mk_y.append(ref_price * 1.012)
            mk_text.append(n["title"])
    if mk_x:
        fig.add_trace(go.Scatter(
            x=mk_x, y=mk_y, mode="markers", name="📰新闻",
            marker=dict(symbol="diamond", size=11, color="#f2b84b", line=dict(width=1, color=bg)),
            text=mk_text, hovertemplate="📰 %{text}<extra></extra>",
        ), row=1, col=1)

    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[16, 9.5], pattern="hour")])
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_xaxes(rangeslider_visible=True, rangeslider_thickness=0.06, row=2, col=1)
    fig.update_yaxes(title_text="价格 ($)", side="right", row=1, col=1)
    fig.update_yaxes(title_text="成交量", side="right", row=2, col=1)
    fig.update_layout(
        template=template, height=520, margin=dict(l=10, r=50, t=40, b=10),
        showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        dragmode="pan",
    )
    st.plotly_chart(fig, use_container_width=True, config={
        "scrollZoom": True, "displaylogo": False,
        "modeBarButtonsToAdd": ["drawline", "drawopenpath", "drawrect", "drawcircle", "eraseshape"],
    })
    st.caption("滚轮/中间拖动=整体缩放平移；鼠标放最右边价格数字上下拖=单独缩放价格轴。🟡菱形=新闻，悬停看标题。")

st.markdown("---")
st.caption("行情来自 Twelve Data（可能有延迟），新闻来自 Yahoo Finance，AI分析来自 Google Gemini。仅供个人参考，不构成投资建议。")
