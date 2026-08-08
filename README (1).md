# 盘中异动 · 美股新闻联动看板（Streamlit 版）

只有3个文件：`streamlit_app.py`（主程序）、`requirements.txt`（依赖清单）、`README.md`（本文档）。

## 关于"会不会不小心花钱"——先说结论

**不会。** 这套东西从头到尾没有任何地方需要绑定信用卡：
- Twelve Data 免费版注册不需要卡，超出免费额度（每天800次/每分钟8次）只会让请求失败报错，**不会自动升级或扣费**
- Streamlit Community Cloud 部署和使用完全免费，不需要绑卡
- yfinance 本身不需要注册，也就没有任何付费的可能

## 第一步：注册 Twelve Data（免费）

1. 打开 https://twelvedata.com/register 注册（不需要信用卡）
2. 登录后在 Dashboard 能看到 API Key，复制保存好

## 第二步：建一个新的 GitHub 仓库

1. 去 https://github.com → 右上角 "+" → "New repository"
2. 名字随便取（比如 `stock-streamlit`），选 **Public**，点 Create

## 第三步：上传3个文件

1. 进入刚建好的仓库 → 点绿色 "uploading an existing file" 链接（或 "Add file" → "Upload files"）
2. 把 `streamlit_app.py`、`requirements.txt`、`README.md` 拖进方框
3. 滑到底部，点 "Commit changes"

## 第四步：去 Streamlit 部署

1. 打开 https://share.streamlit.io ，用 GitHub 账号登录（一键授权即可）
2. 点 "New app" / "Create app"
3. 选你刚才那个仓库，Branch 选 `main`，Main file path 填 `streamlit_app.py`
4. 点 "Deploy"，等1-2分钟

## 第五步：填入 Twelve Data 的 Key（不会公开显示）

1. App 部署好之后，右下角找到 "⋮"（更多选项）→ "Settings"
2. 左侧找到 "Secrets"
3. 在文本框里填一行（注意保留引号）：
   ```
   TWELVE_DATA_API_KEY = "你的key"
   ```
4. 点 "Save"，App 会自动重启，等半分钟左右

## 第六步：打开使用

App 的网址长这样：`https://你取的名字.streamlit.app`

- 左侧有6支默认股票的快捷按钮
- 也可以在"查任意股票"那里直接打字输入任何美股代码（比如 GOOGL、AMD、PLTR），点查询，几秒钟就有结果
- 可以勾选"显示均线"、"显示阻力/支撑线"
- 顶部可以切换"深色/浅色"背景

## 常见问题

**"查询失败"怎么办？**
最常见是股票代码打错了（要用美股官方代码，比如"谷歌"要打 `GOOGL` 不是"谷歌"），或者短时间内查太多次触发了限速（等一分钟再试）。

**App 打开很慢/显示"正在唤醒"？**
免费版超过12小时没人访问会"睡眠"，下次打开需要等二三十秒重新启动，属于正常现象，等一下就好。

**以后想加更多默认股票？**
打开 `streamlit_app.py`，找到最上面 `DEFAULT_WATCHLIST = [...]` 这一行，加你想要的代码即可（不过其实不用改这个也能查——直接在"查任意股票"框里输入就行）。

**这个跟之前 GitHub Pages 那套的区别？**
这套是"现场实时查"，之前那套是"每天定时预先抓好几支"。这套更灵活（能查任意股票），但每次打开时都要等它现场去请求数据（几秒钟），之前那套是提前抓好、打开秒显示。之前那套（`.github/workflows` 那些文件）如果不用了，可以留着不管，也可以直接删掉那个仓库。
