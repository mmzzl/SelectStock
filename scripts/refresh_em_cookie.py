"""用 headless Chrome 过一遍东财资金流页面，让站点 JS 自己种 Cookie，落盘给采集脚本用。

为什么要跑 JS（2026-10-20 实测）：
- push2 接口被掐不掐 UA/域名（六个镜像同时断，闸按 IP 时间窗计），但**同一时间窗内**
  带站点 JS 种的 Cookie 存活率显著高：页 20-34、0.8s/页的硬窗口边缘，
  裸 curl_cffi 7/15（页 27 起连败），带 Cookie 14/15。
- 这些 Cookie（qgqp_b_id/st_si/nid18/gviem/st_pvi…）**只由页面 JS 生成**：
  五个端点的 HTTP 响应全部 0 个 Set-Cookie，纯 requests 拿不到，必须起浏览器。

链路：selenium(Selenium Manager 自动配 driver) + 系统 google-chrome，
`--headless=new` + 独立临时 user-data-dir（不碰用户浏览器 profile）。

用法:
    python scripts/refresh_em_cookie.py                 # 写 data/fund_flow/em_cookie.txt
    python scripts/refresh_em_cookie.py --out /tmp/c.txt --wait 6
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 fetch_fund_flow.COOKIE_PATH 同值（scripts/ 不是包，不跨脚本 import，改动要两边同步）
COOKIE_PATH = ROOT / "data" / "fund_flow" / "em_cookie.txt"

logger = logging.getLogger("em_cookie")

PAGE_URL = "https://data.eastmoney.com/zjlx/detail.html"
# 浏览器种 Cookie 前先访问的预热站点接口（无害 GET，让域名连接与首次会话建立）
SKIP_NAMES = {"st_asi"}   # 值为 "delete" 的删除标记，带上反而怪


def seed_cookie_string(*, wait: float = 4.0, url: str = PAGE_URL) -> str:
    """起 headless Chrome 打开资金流页面，等 JS 种 Cookie，返回可放进请求头的 Cookie 串。"""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage"):
        opts.add_argument(flag)
    with tempfile.TemporaryDirectory(prefix="em_probe_") as profile:
        opts.add_argument(f"--user-data-dir={profile}")
        driver = webdriver.Chrome(options=opts)
        try:
            driver.set_page_load_timeout(45)
            driver.get(url)
            time.sleep(wait)
            pairs = [(c["name"], c["value"]) for c in driver.get_cookies()
                     if c["name"] not in SKIP_NAMES and c["value"]]
        finally:
            driver.quit()
    if not pairs:
        raise RuntimeError("页面没种出任何 Cookie（结构变了或被拦了），不落盘")
    return "; ".join(f"{k}={v}" for k, v in pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(COOKIE_PATH),
                        help="Cookie 落盘路径（默认 data/fund_flow/em_cookie.txt）")
    parser.add_argument("--wait", type=float, default=4.0, help="页面加载后等 JS 的秒数")
    parser.add_argument("--url", default=PAGE_URL)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cookie = seed_cookie_string(wait=args.wait, url=args.url)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(cookie, encoding="utf-8")
    tmp.replace(out)
    names = [p.split("=", 1)[0] for p in cookie.split("; ")]
    logger.info("Cookie 落盘 %s（%d 项：%s）", out, len(names), ",".join(sorted(names)[:6]) + "…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
