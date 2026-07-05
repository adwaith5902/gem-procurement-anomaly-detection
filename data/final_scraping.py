"""
GEM CONTRACT SCRAPER v3.2 — SMARTER CAPTCHA/RESULT HANDLING
==============================================================
Builds on v3.1 (deeper search + daily loop). Same CaptchaSolver, same
parsing, same DBWriter schema, same scroll/no-growth logic for days that
DO have contracts.

WHAT CHANGED FROM v3.1 (root-cause fix for "CAPTCHA accepted first try,
then suddenly fails 15/15 times")
---------------------------------------------------------------------
Diagnosis from the log: every single CAPTCHA was read confidently by
ddddocr ('4z37bn','hf39w3', etc.) on EVERY attempt. The days that failed
15/15 (13-01-2024, 14-01-2024, 20-01-2024) are all Sat/Sun — GeM
genuinely returns ZERO contracts for those dates. The old code only
checked for div.border.block, saw none, assumed "CAPTCHA wrong", refreshed
+ retried 15× at 55s each (page_load_wait=50s) = ~14 min wasted, then
requeued the SAME day to fail the same way again. The 14-min stuck state
is also the most likely cause of the later InvalidSessionId / NoSuchWindow
crashes.

Fixes:
  1. page_load_wait default 50s -> 8s (GeM's AJAX responds in 1-3s when
     there IS data — no reason to wait 50s when there isn't).
  2. NEW _wait_for_page_response() polls for ALL THREE signals in the same
     loop (results blocks, "no record(s) found" text, CAPTCHA error text),
     returning as soon as any is detected or the timeout expires. This
     replaces the old sequential approach where _results_visible waited the
     FULL page_load_wait seconds before "no records" text was even checked.
     Net effect: a zero-results day resolves in ~1.3 s instead of 8+ s.
  3. NEW ambiguous-attempt counter. An attempt is "ambiguous" only if the
     poll loop timed out with NO signal at all. After `ambiguous_threshold`
     (default 5) such timeouts with NO explicit captcha error ever seen,
     the day is ACCEPTED as 0 contracts (logged clearly + tagged
     'zero_assumed' in the DB for later review) instead of burning all 15
     tries. A genuinely wrong-but-error-shown CAPTCHA still gets all
     max_tries attempts — only the "no signal at all" case short-circuits.
  4. _scrape_window: if page 1 already has 0 contracts, the scroll loop
     is skipped entirely (nothing to scroll for).
  5. Alert-safety added throughout FormAutomator (_find,
     _xpath_date_fallback, _refresh_captcha, _wait_for_page_response,
     _js_set, _get_captcha_bytes, _clear_and_type_captcha, _click_search,
     _body_text_lower) — a stray "Please try again after some time" alert
     is dismissed and the operation retried instead of silently returning
     empty / crashing.
  6. completed_windows table gets a `note` column:
       'normal'         -> scraped with contracts (scrolled to 0 growth)
       'zero_confirmed' -> page said "no records found" explicitly
       'zero_assumed'   -> 0 after `ambiguous_threshold` clean timeouts,
                           no captcha error ever shown — review if unsure
     print_summary() lists any 'zero_assumed' dates for spot-checking.

USAGE (same as v3.1, plus two new flags):
  python gem_scraper_auto_v3.2.py --from 01-01-2024 --to 31-12-2024 --workers 4
  python gem_scraper_auto_v3.2.py --from 01-01-2024 --to 31-01-2024 \\
      --page-load-wait 8 --ambiguous-threshold 5

  # Re-run the same command anytime to resume (completed days skipped).
"""

from __future__ import annotations

import argparse
import io
import logging
import queue
import re
import sqlite3
import sys
import threading
import time
import random
from datetime import datetime, timedelta
from typing import Optional

from bs4 import BeautifulSoup
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoAlertPresentException,
    NoSuchElementException,
    NoSuchWindowException,
    UnexpectedAlertPresentException,
    WebDriverException,
)

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

SEARCH_URL = "https://gem.gov.in/view_contracts"

DEFAULT_CONFIG = {
    "from_date":           "01-01-2024",
    "to_date":             "31-12-2024",
    "step_days":           1,
    "category":            "",
    "db_path":             "gem_contracts.db",
    "headless":            False,
    "num_workers":         1,
    "scroll_pause":        0.15,
    "jump_pause":          0.6,
    "no_growth_limit":     8,
    "no_growth_base_wait": 1.0,
    "no_growth_max_wait":  10.0,
    "max_cycles":          9999,
    "max_captcha_tries":   15,
    "captcha_wait":        1.0,
    "db_queue_maxsize":    500,
    "flush_interval":      5.0,
    "page_load_wait":      8,       # was 50 in v3.1 — GeM AJAX is fast (1-3s)
    "ambiguous_threshold": 5,       # clean timeouts before assuming 0 contracts
    "rate_limit_wait":     300,
    "restart_every":       8,
}

# ─────────────────────────────────────────────────────────────────────────────
# GEM FORM CSS SELECTORS
# ─────────────────────────────────────────────────────────────────────────────

SELECTORS = {
    "from_date": [
        "input#from_date", "input[name='from_date']", "input#fromDate",
        "input[name='fromDate']", "input[placeholder*='From']",
        "input[placeholder*='from']", "input[id*='from'][type='date']",
        "input[id*='from'][type='text']", "input[name*='from'][type='text']",
        "input[name*='start']", "input[id*='start']",
        ".daterangepicker input:first-of-type",
    ],
    "to_date": [
        "input#to_date", "input[name='to_date']", "input#toDate",
        "input[name='toDate']", "input[placeholder*='To']",
        "input[placeholder*='to']", "input[id*='to'][type='date']",
        "input[id*='to'][type='text']", "input[name*='to'][type='text']",
        "input[name*='end']", "input[id*='end']",
        ".daterangepicker input:last-of-type",
    ],
    "captcha_img": [
        "img[src*='captcha']", "img[id*='captcha']", "img[alt*='captcha']",
        "img[class*='captcha']", ".captcha-image img", "#captchaImage",
        "#captcha_image", "img[src*='generate']", "img[src*='image']",
    ],
    "captcha_input": [
        "input[name*='captcha']", "input[id*='captcha']",
        "input[placeholder*='captcha']", "input[placeholder*='Captcha']",
        "input[placeholder*='code']", "input[placeholder*='Code']",
        "input[name='captcha_response']", "#captchaInput", "#captcha_input",
        "input[type='text'][name*='cap']",
    ],
    "captcha_refresh": [
        "img[onclick*='captcha']", "a[href*='captcha']", "span[onclick*='captcha']",
        ".captcha-refresh", "#refreshCaptcha", "img[title*='refresh']",
        "img[title*='Refresh']", "[onclick*='refreshCaptcha']",
        "[onclick*='refresh_captcha']", "[id*='refresh'][id*='captcha']",
    ],
    "search_btn": [
        "button[type='submit']", "input[type='submit']", "button#search",
        "button[id*='search']", "input[id*='search']", "#searchButton",
        "#search_btn", ".btn-search", "button[class*='search']",
        "button[class*='btn-primary']", "button[class*='btn']",
    ],
    "results": [
        "div.border.block", "div[class*='border'][class*='block']",
        ".contract-block", "[class*='contract']",
    ],
    "category": [
        "select[name*='category']", "select[id*='category']", "#category",
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def make_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s  %(message)s")
        fh = logging.FileHandler("gem_scraper.log", encoding="utf-8")
        fh.setFormatter(fmt)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

log = make_logger("GEM")

# ─────────────────────────────────────────────────────────────────────────────
# DATE WINDOW GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_windows(from_str: str, to_str: str, step_days: int) -> list[tuple[str, str]]:
    """
    Split [from_str, to_str] (DD-MM-YYYY) into consecutive windows of
    `step_days` days each. Last window is clipped to to_str if shorter.
    """
    start = datetime.strptime(from_str, "%d-%m-%Y")
    end   = datetime.strptime(to_str,   "%d-%m-%Y")
    if start > end:
        start, end = end, start
    step = max(1, step_days)

    windows = []
    cur = start
    while cur <= end:
        win_end = min(cur + timedelta(days=step - 1), end)
        windows.append((cur.strftime("%d-%m-%Y"), win_end.strftime("%d-%m-%Y")))
        cur = win_end + timedelta(days=1)
    return windows

# ─────────────────────────────────────────────────────────────────────────────
# WINDOW COMPLETION TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class WindowTracker:
    """
    Tracks which (from_date, to_date) windows have been fully scraped, plus
    a `note` describing HOW they completed:
      'normal' | 'zero_confirmed' | 'zero_assumed'
    Thread-safe via a lock + short-lived connections (WAL mode).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS completed_windows (
                from_date TEXT, to_date TEXT,
                contracts INTEGER, completed_at TEXT,
                note TEXT DEFAULT 'normal',
                PRIMARY KEY (from_date, to_date)
            )
        """)
        # Migrate older DBs that don't have the `note` column yet
        cols = [r[1] for r in
                conn.execute("PRAGMA table_info(completed_windows)").fetchall()]
        if "note" not in cols:
            conn.execute(
                "ALTER TABLE completed_windows ADD COLUMN note TEXT DEFAULT 'normal'")
        conn.commit()
        conn.close()

    def is_done(self, from_date: str, to_date: str) -> bool:
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT 1 FROM completed_windows WHERE from_date=? AND to_date=?",
                (from_date, to_date),
            ).fetchone()
            conn.close()
            return row is not None

    def mark_done(self, from_date: str, to_date: str,
                  contracts: int, note: str = "normal"):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT OR REPLACE INTO completed_windows VALUES (?,?,?,?,?)",
                (from_date, to_date, contracts,
                 datetime.now().isoformat(), note),
            )
            conn.commit()
            conn.close()

    def summary(self) -> tuple[int, int]:
        """Returns (windows_done, total_contracts_across_done_windows)."""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(contracts),0) "
                "FROM completed_windows"
            ).fetchone()
            conn.close()
            return row[0], row[1]

    def zero_assumed_dates(self) -> list[tuple[str, str]]:
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT from_date,to_date FROM completed_windows "
                "WHERE note='zero_assumed' ORDER BY completed_at"
            ).fetchall()
            conn.close()
            return rows

# ─────────────────────────────────────────────────────────────────────────────
# FREE CAPTCHA SOLVER
# ─────────────────────────────────────────────────────────────────────────────

class CaptchaSolver:
    """
    Solves simple alphanumeric CAPTCHAs using locally-installed libraries.
    Priority: ddddocr → easyocr → pytesseract.
    """

    def __init__(self):
        self._dddd = None
        self._easy = None
        self._tess = False
        self._log  = make_logger("CAPTCHA")
        self._init()

    def _init(self):
        try:
            import ddddocr
            self._dddd = ddddocr.DdddOcr(show_ad=False)
            self._log.info("ddddocr ✓  (primary solver)")
        except ImportError:
            self._log.info("ddddocr not installed  →  pip install ddddocr")
        except Exception as e:
            self._log.warning(f"ddddocr init error: {e}")

        try:
            import easyocr
            self._easy = easyocr.Reader(["en"], verbose=False)
            self._log.info("easyocr ✓  (secondary solver)")
        except ImportError:
            self._log.info("easyocr not installed  →  pip install easyocr")
        except Exception as e:
            self._log.warning(f"easyocr init error: {e}")

        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._tess = True
            self._log.info("pytesseract ✓  (tertiary solver)")
        except Exception:
            self._log.info(
                "pytesseract not found  →  "
                "install Tesseract binary + pip install pytesseract")

        if not self._dddd and not self._easy and not self._tess:
            raise RuntimeError(
                "\nNo CAPTCHA solver available!\n"
                "Install at least one:\n"
                "  pip install ddddocr          ← recommended, no extras needed\n"
                "  pip install pytesseract      ← needs Tesseract binary\n"
                "  pip install easyocr          ← large download (~200 MB)\n"
            )

    @staticmethod
    def _preprocess(image_bytes: bytes) -> tuple[bytes, Image.Image]:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        img = img.resize((w * 3, h * 3), Image.LANCZOS)
        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        img = img.filter(ImageFilter.SHARPEN)
        img = ImageOps.autocontrast(img)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img

    def _try_ddddocr(self, raw_bytes: bytes) -> str:
        if not self._dddd:
            return ""
        try:
            text = self._dddd.classification(raw_bytes)
            return re.sub(r"\s+", "", text).strip()
        except Exception as e:
            self._log.debug(f"ddddocr error: {e}")
            return ""

    def _try_easyocr(self, img: Image.Image) -> str:
        if not self._easy:
            return ""
        try:
            import numpy as np
            arr    = np.array(img.convert("RGB"))
            result = self._easy.readtext(arr, detail=0, paragraph=True)
            text   = "".join(result)
            return re.sub(r"\s+", "", text).strip()
        except Exception as e:
            self._log.debug(f"easyocr error: {e}")
            return ""

    def _try_tesseract(self, img: Image.Image) -> str:
        if not self._tess:
            return ""
        try:
            import pytesseract
            cfg  = (
                "--psm 8 --oem 3 "
                "-c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            )
            text = pytesseract.image_to_string(img, config=cfg)
            return re.sub(r"\s+", "", text).strip()
        except Exception as e:
            self._log.debug(f"tesseract error: {e}")
            return ""

    def solve(self, image_bytes: bytes) -> str:
        processed_bytes, processed_img = self._preprocess(image_bytes)
        candidates = []

        t = self._try_ddddocr(image_bytes)
        if t:
            candidates.append(("ddddocr-raw", t))

        t2 = self._try_ddddocr(processed_bytes)
        if t2 and t2 != t:
            candidates.append(("ddddocr-proc", t2))

        t = self._try_easyocr(processed_img)
        if t:
            candidates.append(("easyocr", t))

        t = self._try_tesseract(processed_img)
        if t:
            candidates.append(("tesseract", t))

        if not candidates:
            self._log.warning("All solvers returned empty — will retry with new CAPTCHA")
            return ""

        self._log.info(f"CAPTCHA candidates: {candidates}")
        from collections import Counter
        freq = Counter(v for _, v in candidates)
        best = freq.most_common(1)[0][0]
        self._log.info(f"CAPTCHA answer: '{best}'")
        return best

# ─────────────────────────────────────────────────────────────────────────────
# ALERT-SAFE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _dismiss_alert(driver: webdriver.Chrome,
                   alert_log: Optional[logging.Logger] = None) -> str:
    """Accept any open JS alert and return its text ('' if none present)."""
    try:
        alert = driver.switch_to.alert
        text  = ""
        try:
            text = alert.text or ""
        except Exception:
            pass
        try:
            alert.accept()
        except Exception:
            try:
                alert.dismiss()
            except Exception:
                pass
        if alert_log and text:
            alert_log.warning(f"Dismissed alert: {text!r}")
        return text
    except NoAlertPresentException:
        return ""


def safe_execute_script(driver: webdriver.Chrome, script: str, *args,
                        retries: int = 4, rate_limit_wait: int = 300,
                        alert_log: Optional[logging.Logger] = None):
    """
    driver.execute_script() that survives 'Please try again after some time'
    and other unexpected alerts. On a rate-limit alert waits `rate_limit_wait`
    seconds before retrying; on other alerts waits briefly.
    Returns the script's return value, or None if all retries failed.
    """
    for attempt in range(retries):
        try:
            return driver.execute_script(script, *args)
        except UnexpectedAlertPresentException:
            text = _dismiss_alert(driver, alert_log)
            if "try again" in text.lower():
                if alert_log:
                    alert_log.warning(f"Rate-limited — waiting {rate_limit_wait}s")
                time.sleep(rate_limit_wait)
            else:
                time.sleep(2)
        except Exception as e:
            if alert_log:
                alert_log.debug(
                    f"execute_script error ({attempt+1}/{retries}): {e}")
            time.sleep(1.5 * (2 ** attempt))
    return None


def safe_get_source(driver: webdriver.Chrome, retries: int = 5,
                    rate_limit_wait: int = 300,
                    alert_log: Optional[logging.Logger] = None) -> str:
    for attempt in range(retries):
        try:
            return driver.page_source
        except UnexpectedAlertPresentException:
            text = _dismiss_alert(driver, alert_log)
            if "try again" in text.lower():
                if alert_log:
                    alert_log.warning(f"Rate-limited — waiting {rate_limit_wait}s")
                time.sleep(rate_limit_wait)
            else:
                time.sleep(1.5 * (2 ** attempt))
        except Exception as e:
            if alert_log:
                alert_log.warning(f"page_source error ({attempt+1}): {e}")
            time.sleep(1.5 * (2 ** attempt))
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# FORM AUTOMATOR
# ─────────────────────────────────────────────────────────────────────────────

class FormAutomator:

    # Phrases that mean "the search ran fine but nothing to show"
    NO_RESULTS_PHRASES = [
        "no record found", "no records found", "no data found",
        "no contract found", "no contracts found", "no result found",
        "no results found", "record not found", "records not found",
        "no matching record", "data not available", "0 record",
        "no data available", "nothing found", "no record(s) found",
    ]

    # Phrases that mean "the CAPTCHA itself was rejected"
    CAPTCHA_ERROR_PHRASES = [
        "invalid captcha", "wrong captcha", "captcha incorrect",
        "please enter valid", "captcha error", "verification failed",
        "invalid code", "wrong code", "incorrect captcha",
        "captcha does not match", "captcha mismatch",
    ]

    def __init__(self, driver: webdriver.Chrome,
                 captcha_solver: CaptchaSolver, config: dict):
        self.driver = driver
        self.solver = captcha_solver
        self.cfg    = config
        self.log    = make_logger("FORM")
        # Set True when a window is accepted as 0 contracts via the
        # ambiguous-threshold path (no explicit "no records" confirmation).
        self._zero_assumed = False

    # ── element finder (alert-safe) ───────────────────────────────────────────

    def _find(self, key: str):
        for sel in SELECTORS.get(key, []):
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el and el.is_displayed():
                    self.log.debug(f"Found '{key}' via: {sel}")
                    return el
            except NoSuchElementException:
                continue
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
                continue
            except Exception:
                continue
        return None

    # ── JS value setter (alert-safe) ──────────────────────────────────────────

    def _js_set(self, element, value: str):
        script = """
            const el=arguments[0], val=arguments[1];
            el.removeAttribute('readonly'); el.removeAttribute('disabled');
            el.focus(); el.value=val;
            ['input','change','keyup','keydown','blur'].forEach(function(ev){
                el.dispatchEvent(new Event(ev,{bubbles:true,cancelable:true}));
            });
            const nativeInput=Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype,'value');
            if(nativeInput && nativeInput.set){
                nativeInput.set.call(el,val);
                el.dispatchEvent(new Event('input',{bubbles:true}));
            }
        """
        try:
            self.driver.execute_script(script, element, value)
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
            try:
                self.driver.execute_script(script, element, value)
            except Exception:
                pass
        except Exception:
            pass

    # ── date conversion ───────────────────────────────────────────────────────

    @staticmethod
    def _gem_date_variants(date_str: str) -> list[str]:
        try:
            d = datetime.strptime(date_str, "%d-%m-%Y")
            return [
                d.strftime("%d-%m-%Y"), d.strftime("%d/%m/%Y"),
                d.strftime("%Y-%m-%d"), d.strftime("%m/%d/%Y"),
                d.strftime("%d %b %Y"), d.strftime("%d %B %Y"),
            ]
        except ValueError:
            return [date_str]

    # ── fill dates ────────────────────────────────────────────────────────────

    def fill_dates(self, from_date: str, to_date: str) -> bool:
        from_el = self._find("from_date")
        to_el   = self._find("to_date")

        if not from_el and not to_el:
            from_el, to_el = self._xpath_date_fallback()

        if not from_el:
            self.log.error(
                "Could not find FROM date field — portal layout may have changed")
            return False

        filled_from = False
        for variant in self._gem_date_variants(from_date):
            self._js_set(from_el, variant)
            time.sleep(0.1)
            try:
                actual = from_el.get_attribute("value") or ""
                if actual.strip():
                    self.log.debug(
                        f"FROM date filled: '{actual}' (format: {variant!r})")
                    filled_from = True
                    break
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
            except Exception:
                pass
        if not filled_from:
            self.log.warning("FROM date may not have been accepted. Proceeding anyway.")

        if to_el:
            for variant in self._gem_date_variants(to_date):
                self._js_set(to_el, variant)
                time.sleep(0.1)
                try:
                    actual = to_el.get_attribute("value") or ""
                    if actual.strip():
                        self.log.debug(
                            f"TO date filled: '{actual}' (format: {variant!r})")
                        break
                except UnexpectedAlertPresentException:
                    _dismiss_alert(self.driver, self.log)
                except Exception:
                    pass
        else:
            self.log.warning("TO date field not found — FROM date only filled.")

        return True

    def _xpath_date_fallback(self):
        """XPath-based search for date inputs — alert-safe."""
        from_el = to_el = None
        xpaths_from = [
            "//label[contains(translate(.,'from','FROM'),'FROM')]"
            "/following::input[1]",
            "//td[contains(translate(.,'from','FROM'),'FROM')]"
            "/following::input[1]",
            "//span[contains(translate(.,'from','FROM'),'FROM')]"
            "/following::input[1]",
        ]
        xpaths_to = [
            "//label[contains(translate(.,'to','TO'),'TO')]"
            "/following::input[1]",
            "//td[contains(translate(.,'to','TO'),'TO')]"
            "/following::input[1]",
            "//span[contains(translate(.,'to','TO'),'TO')]"
            "/following::input[1]",
        ]
        for xp in xpaths_from:
            try:
                el = self.driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    from_el = el
                    break
            except NoSuchElementException:
                continue
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
                continue
            except Exception:
                continue
        for xp in xpaths_to:
            try:
                el = self.driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    to_el = el
                    break
            except NoSuchElementException:
                continue
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
                continue
            except Exception:
                continue
        return from_el, to_el

    # ── category ──────────────────────────────────────────────────────────────

    def fill_category(self, category: str):
        if not category:
            return
        el = self._find("category")
        if el:
            try:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_visible_text(category)
                    self.log.info(f"Category selected: {category}")
                except Exception:
                    sel.select_by_value(category)
            except Exception as e:
                self.log.warning(f"Could not set category: {e}")

    # ── CAPTCHA image / refresh / type / click (alert-safe) ──────────────────

    def _get_captcha_bytes(self) -> bytes | None:
        img_el = self._find("captcha_img")
        if not img_el:
            self.log.warning("CAPTCHA image element not found")
            return None
        try:
            png = img_el.screenshot_as_png
            if png:
                return png
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
        except Exception as e:
            self.log.debug(f"Element screenshot failed: {e}")
        try:
            import requests
            src = img_el.get_attribute("src") or ""
            if not src:
                return None
            if src.startswith("//"):
                src = "https:" + src
            elif not src.startswith("http"):
                src = "https://gem.gov.in" + src
            cookies = {c["name"]: c["value"] for c in self.driver.get_cookies()}
            resp = requests.get(src, cookies=cookies,
                                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if resp.ok and resp.content:
                return resp.content
        except Exception as e:
            self.log.debug(f"CAPTCHA download failed: {e}")
        return None

    def _refresh_captcha(self):
        _dismiss_alert(self.driver, self.log)  # proactive
        refresh_el = self._find("captcha_refresh")
        if refresh_el:
            try:
                self.driver.execute_script("arguments[0].click();", refresh_el)
                time.sleep(0.8)
                return
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
            except Exception:
                pass
        try:
            self.driver.execute_script(
                "if(typeof refreshCaptcha==='function') refreshCaptcha();"
                "else if(typeof reload_captcha==='function') reload_captcha();"
            )
            time.sleep(0.8)
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
        except Exception:
            pass

    def _clear_and_type_captcha(self, text: str):
        inp = self._find("captcha_input")
        if not inp:
            self.log.error("CAPTCHA input field not found")
            return
        try:
            inp.clear()
            inp.click()
            inp.send_keys(text)
            self.log.debug(f"Typed CAPTCHA: '{text}'")
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
            self._js_set(inp, text)
        except Exception:
            self._js_set(inp, text)

    def _click_search(self):
        btn = self._find("search_btn")
        if btn:
            try:
                self.driver.execute_script("arguments[0].click();", btn)
                return
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
            except Exception:
                pass
        try:
            form = self.driver.find_element(By.TAG_NAME, "form")
            self.driver.execute_script("arguments[0].submit();", form)
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
        except Exception as e:
            self.log.error(f"Could not click Search: {e}")

    # ── shared body-text fetch (alert-safe, with retry) ───────────────────────

    def _body_text_lower(self) -> str:
        """
        Return the page body text, lower-cased.
        If an alert fires, dismiss it and retry ONCE so callers see the
        actual page content rather than an empty string.
        """
        try:
            return self.driver.find_element(By.TAG_NAME, "body").text.lower()
        except UnexpectedAlertPresentException:
            _dismiss_alert(self.driver, self.log)
            # Retry after the alert is gone
            try:
                return self.driver.find_element(
                    By.TAG_NAME, "body").text.lower()
            except Exception:
                return ""
        except Exception:
            return ""

    # ── CORE: unified polling function ────────────────────────────────────────

    def _wait_for_page_response(self, timeout: float = 8.0) -> str:
        """
        Poll the live page every 0.3 s and return the FIRST signal detected:

          'results'       — at least one div.border.block is visible
          'no_results'    — "no record(s) found" style message is in the body
          'captcha_error' — an explicit CAPTCHA-rejection message is in the body
          'timeout'       — none of the above appeared within `timeout` seconds

        Checking all three together is the key fix over v3.1's sequential
        approach (_results_visible waited the FULL page_load_wait before
        "no records" text was even looked at). Now a zero-results day that
        would have taken 8 s per attempt resolves in ~1.3 s.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:

            # 1. Result blocks
            try:
                if self.driver.find_elements(
                        By.CSS_SELECTOR, "div.border.block"):
                    return "results"
                if self.driver.find_elements(
                        By.XPATH,
                        "//*[contains(@class,'block')"
                        " and contains(@class,'border')]"):
                    return "results"
            except UnexpectedAlertPresentException:
                _dismiss_alert(self.driver, self.log)
            except Exception:
                pass

            # 2. "No results" or CAPTCHA-error text
            body = self._body_text_lower()
            if body:
                if any(p in body for p in self.NO_RESULTS_PHRASES):
                    return "no_results"
                if any(p in body for p in self.CAPTCHA_ERROR_PHRASES):
                    return "captcha_error"

            time.sleep(0.3)

        return "timeout"

    # ── Convenience wrappers (kept for testability) ───────────────────────────

    def _no_results_text_present(self) -> bool:
        body = self._body_text_lower()
        return any(p in body for p in self.NO_RESULTS_PHRASES)

    def _captcha_failed(self) -> bool:
        body = self._body_text_lower()
        return any(p in body for p in self.CAPTCHA_ERROR_PHRASES)

    # ── main auto-submit ──────────────────────────────────────────────────────

    def auto_fill_and_submit(self, from_date: str, to_date: str,
                             category: str = "") -> str | None:
        """
        Returns:
          - HTML string on success (includes legitimate "0 contracts" case)
          - None only if CAPTCHA was explicitly rejected every single attempt

        self._zero_assumed is set True when 0 contracts was concluded via
        the ambiguous-timeout threshold (no explicit confirmation) — caller
        tags this for later review.
        """
        max_tries           = self.cfg.get("max_captcha_tries", 15)
        ambiguous_threshold = self.cfg.get("ambiguous_threshold", 5)
        page_load_wait      = self.cfg.get("page_load_wait", 8)
        self._zero_assumed  = False

        if not self.fill_dates(from_date, to_date):
            self.log.error("Date filling failed — cannot proceed")
            return None

        if category:
            self.fill_category(category)

        ambiguous_count = 0

        for attempt in range(1, max_tries + 1):
            _dismiss_alert(self.driver, self.log)  # proactive, every attempt
            self.log.info(f"CAPTCHA attempt {attempt}/{max_tries}")

            img_bytes = self._get_captcha_bytes()
            if not img_bytes:
                self.log.warning("Could not get CAPTCHA image — refreshing")
                self._refresh_captcha()
                time.sleep(0.5)
                continue

            answer = self.solver.solve(img_bytes)
            if not answer:
                self.log.warning("Solver returned empty string — refreshing")
                self._refresh_captcha()
                time.sleep(0.5)
                continue

            self._clear_and_type_captcha(answer)
            time.sleep(0.2)
            self._click_search()
            time.sleep(self.cfg.get("captcha_wait", 1.0))

            response = self._wait_for_page_response(timeout=page_load_wait)

            # ── 1) result blocks visible → SUCCESS, has contracts ─────────
            if response == "results":
                self.log.info(
                    f"✓ CAPTCHA solved, results found (attempt {attempt})")
                return safe_get_source(
                    self.driver, alert_log=self.log,
                    rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

            # ── 2) explicit "no records" text → SUCCESS, 0 contracts ──────
            if response == "no_results":
                self.log.info(
                    f"✓ CAPTCHA solved — page reports NO RECORDS for "
                    f"{from_date}→{to_date} (attempt {attempt})")
                self._zero_assumed = False   # confirmed, not assumed
                return safe_get_source(
                    self.driver, alert_log=self.log,
                    rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

            # ── 3) explicit CAPTCHA error → genuinely wrong, retry ────────
            if response == "captcha_error":
                self.log.info(
                    f"✗ Wrong CAPTCHA (explicit error shown) — attempt {attempt}")
                self._refresh_captcha()
                time.sleep(0.5)
                self.fill_dates(from_date, to_date)
                if category:
                    self.fill_category(category)
                continue

            # ── 4) timeout — no signal at all (ambiguous) ─────────────────
            ambiguous_count += 1
            self.log.info(
                f"? Ambiguous (poll timed out: no blocks, no error, "
                f"no 'no-results' text) — "
                f"attempt {attempt}, ambiguous {ambiguous_count}/"
                f"{ambiguous_threshold}")

            if ambiguous_count >= ambiguous_threshold:
                self.log.warning(
                    f"⚠️  {ambiguous_count} clean timeouts (CAPTCHA never "
                    f"rejected, but no results and no 'no records' message) — "
                    f"assuming {from_date}→{to_date} has 0 contracts "
                    f"(e.g. weekend/holiday). Tagged 'zero_assumed' for review.")
                html = safe_get_source(
                    self.driver, alert_log=self.log,
                    rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
                self._zero_assumed = True
                return html

            self._refresh_captcha()
            time.sleep(0.5)
            self.fill_dates(from_date, to_date)
            if category:
                self.fill_category(category)

        self.log.error(
            f"CAPTCHA solving failed after {max_tries} attempts for "
            f"{from_date}→{to_date} (CAPTCHA explicitly rejected each time)")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# THREADED DATABASE WRITER
# ─────────────────────────────────────────────────────────────────────────────

class DBWriter(threading.Thread):
    SENTINEL = object()

    def __init__(self, db_path: str, config: dict):
        super().__init__(name="DBWriter", daemon=True)
        self.db_path        = db_path
        self.flush_interval = config.get("flush_interval", 5.0)
        self.q              = queue.Queue(
            maxsize=config.get("db_queue_maxsize", 500))
        self._total_saved   = 0
        self._lock          = threading.Lock()

    @property
    def total_saved(self) -> int:
        with self._lock:
            return self._total_saved

    def push(self, records: list[dict]):
        for r in records:
            self.q.put(r)

    def stop(self):
        self.q.put(self.SENTINEL)
        self.join()

    def run(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._schema(conn)
        buffer     = []
        last_flush = time.time()
        while True:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                item = None
            if item is self.SENTINEL:
                if buffer:
                    n = self._flush(conn, buffer)
                    with self._lock:
                        self._total_saved += n
                conn.close()
                return
            if item is not None:
                buffer.append(item)
            now = time.time()
            if buffer and (len(buffer) >= 100
                           or now - last_flush >= self.flush_interval):
                n = self._flush(conn, buffer)
                with self._lock:
                    self._total_saved += n
                buffer.clear()
                last_flush = now

    def _flush(self, conn: sqlite3.Connection, records: list[dict]) -> int:
        saved = 0
        for r in records:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO contracts
                    (contract_number,org_type,ministry,department,org_name,
                     office_zone,buyer_designation,buying_mode,contract_date,
                     total_value,contract_status,product_name,brand,model,
                     quantity,unit_price,scraped_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    r.get("contract_number"), r.get("org_type"),
                    r.get("ministry"),        r.get("department"),
                    r.get("org_name"),        r.get("office_zone"),
                    r.get("buyer_designation"), r.get("buying_mode"),
                    r.get("contract_date"),   r.get("total_value"),
                    r.get("contract_status"), r.get("product_name"),
                    r.get("brand"),           r.get("model"),
                    r.get("quantity"),        r.get("unit_price"),
                    r.get("scraped_at", datetime.now().isoformat()),
                ))
                saved += 1
            except Exception:
                pass
        conn.commit()
        return saved

    def _schema(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS contracts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_number TEXT UNIQUE,
            org_type        TEXT, ministry TEXT, department TEXT,
            org_name        TEXT, office_zone TEXT,
            buyer_designation TEXT, buying_mode TEXT,
            contract_date   TEXT, total_value REAL,
            contract_status TEXT, product_name TEXT,
            brand TEXT, model TEXT, quantity TEXT,
            unit_price REAL, scraped_at TEXT)""")
        conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# HTML PARSING
# ─────────────────────────────────────────────────────────────────────────────

def clean_price(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[₹\$,\s]", "", str(text)).strip()
    cleaned = re.sub(r"[A-Za-z]", "", cleaned)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _after_label(block: BeautifulSoup, label: str) -> str:
    for strong in block.find_all("strong"):
        if label.lower() in strong.get_text().lower():
            p = strong.parent
            if p:
                txt = p.get_text(strip=True)
                lbl = strong.get_text(strip=True)
                txt = txt[txt.find(lbl) + len(lbl):].lstrip(":").strip()
                return txt
    return ""


def parse_block(block: BeautifulSoup) -> list[dict]:
    contract_number = contract_status = ""
    header = block.find("div", class_="block_header")
    if header:
        txt = header.get_text(" ", strip=True)
        m   = re.search(r"(GEMC-\d+|GEM/\S+)", txt)
        if m:
            contract_number = m.group(1)
        sm = re.search(
            r"Status\s*(?:of\s*the\s*Contract\s*)?[:\-]?\s*(.+)", txt, re.I)
        contract_status = sm.group(1).strip() if sm else txt[20:80]

    org_type = ""
    org_span = block.find("span", class_="ajxtag_buyer_dept_org")
    if org_span:
        org_type = org_span.get_text(strip=True)

    ministry          = _after_label(block, "Ministry")
    department        = _after_label(block, "Department")
    org_name          = _after_label(block, "Organization Name")
    office_zone       = _after_label(block, "Office Zone")
    buyer_designation = _after_label(block, "Buyer Designation")
    buying_mode       = _after_label(block, "Buying Mode")
    contract_date     = _after_label(block, "Contract Date")

    total_value = None
    total_raw   = _after_label(block, "Total")
    if total_raw:
        total_value = clean_price(total_raw)
    if total_value is None:
        m = re.search(
            r"Total\s*[:\s]*[\u20b9]?\s*([\d,\.]+)",
            block.get_text(), re.I)
        if m:
            total_value = clean_price(m.group(1))

    product_rows = []
    table = block.find("table", class_=re.compile(r"table-striped"))
    if table:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if tds:
                product_rows.append([td.get_text(strip=True) for td in tds])
    if not product_rows:
        product_rows = [["", "", "", "", ""]]

    base = dict(
        contract_number=contract_number, org_type=org_type,
        ministry=ministry, department=department, org_name=org_name,
        office_zone=office_zone, buyer_designation=buyer_designation,
        buying_mode=buying_mode, contract_date=contract_date,
        contract_status=contract_status, total_value=total_value,
    )
    records = []
    for cells in product_rows:
        def c(i, _cells=cells):
            return _cells[i] if i < len(_cells) else ""
        records.append({
            **base,
            "product_name": c(0), "brand": c(1), "model": c(2),
            "quantity": c(3), "unit_price": clean_price(c(4)),
            "scraped_at": datetime.now().isoformat(),
        })
    return records


def parse_html_incremental(html: str, seen: set) -> tuple[list[dict], set]:
    soup   = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("div", class_="border block")
    records  = []
    new_seen = set()
    for b in blocks:
        hdr = b.find("div", class_="block_header")
        if not hdr:
            continue
        m  = re.search(r"(GEMC-\d+|GEM/\S+)", hdr.get_text(" ", strip=True))
        cn = m.group(1) if m else None
        if cn and cn in seen:
            continue
        try:
            rows = parse_block(b)
            if rows:
                records.extend(rows)
                if cn:
                    new_seen.add(cn)
        except Exception as e:
            log.debug(f"parse_block error: {e}")
    return records, new_seen

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT — MutationObserver + scroll + direct block-count
# ─────────────────────────────────────────────────────────────────────────────

MUTATION_OBSERVER_JS = """
(function(){
    if(window.__gem_observer_installed) return;
    window.__gem_observer_installed = true;
    window.__gem_new_blocks = false;
    window.__gem_block_count = document.querySelectorAll('div.border.block').length;
    const obs = new MutationObserver(function(){
        const cur = document.querySelectorAll('div.border.block').length;
        if(cur > window.__gem_block_count){
            window.__gem_block_count = cur;
            window.__gem_new_blocks  = true;
        }
    });
    obs.observe(document.body,{childList:true,subtree:true});
})();
"""
CHECK_FLAG_JS = "return window.__gem_new_blocks===true;"
RESET_FLAG_JS = "window.__gem_new_blocks=false;"
BLOCK_COUNT_JS = "return document.querySelectorAll('div.border.block').length;"

SCROLL_JS = """
    window.scrollTo({top:document.body.scrollHeight,behavior:'instant'});
    window.dispatchEvent(new Event('scroll',{bubbles:true}));
    document.dispatchEvent(new Event('scroll',{bubbles:true}));
    var c=document.querySelector('.container,main,#content,.content');
    if(c){c.scrollTop=c.scrollHeight;
          c.dispatchEvent(new Event('scroll',{bubbles:true}));}
"""

JIGGLE_JS = """
    window.scrollTo({top:0,behavior:'instant'});
    window.dispatchEvent(new Event('scroll',{bubbles:true}));
"""

TRIGGER_MORE_JS = """
    var clicked=false;
    var sels=['#load_more','[id*=load]','[class*=load-more]',
              'button[class*=more]','a[class*=more]','button[class*=next]'];
    for(var i=0;i<sels.length&&!clicked;i++){
        var els=document.querySelectorAll(sels[i]);
        for(var j=0;j<els.length;j++){
            var el=els[j];
            if(el.offsetParent!==null){
                var txt=(el.textContent||'').toLowerCase();
                if(txt.includes('more')||txt.includes('load')||
                   txt.includes('next')||el.id==='load_more'){
                    el.click();clicked=true;break;}
            }
        }
    }
    return clicked;
"""

# ─────────────────────────────────────────────────────────────────────────────
# DRIVER SETUP
# ─────────────────────────────────────────────────────────────────────────────

def build_driver(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    svc    = (Service(ChromeDriverManager().install())
              if USE_WDM else Service())
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver

# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING WORKER
# ─────────────────────────────────────────────────────────────────────────────

class ScrapingWorker:
    """
    Owns ONE browser. Repeatedly pulls the next (from_date, to_date) window
    from the shared job_queue, scrapes it fully (deep search + zero-day
    detection), marks it done in the tracker, then moves on.
    Restarts its browser every `restart_every` windows to keep long runs healthy.
    """

    def __init__(self, worker_id: str,
                 job_queue: "queue.Queue[tuple[str,str]]",
                 tracker: WindowTracker, config: dict,
                 db_writer: DBWriter, captcha_solver: CaptchaSolver):
        self.wid       = worker_id
        self.job_queue = job_queue
        self.tracker   = tracker
        self.cfg       = config
        self.writer    = db_writer
        self.solver    = captcha_solver
        self.log       = make_logger(f"W{worker_id}")
        self.driver:    Optional[webdriver.Chrome] = None
        self.automator: Optional[FormAutomator]    = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _start(self):
        self.driver    = build_driver(headless=self.cfg.get("headless", False))
        self.automator = FormAutomator(self.driver, self.solver, self.cfg)

    def _quit(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver    = None
            self.automator = None

    # ── JS helpers (all alert-safe) ───────────────────────────────────────────

    def _install_observer(self):
        safe_execute_script(self.driver, MUTATION_OBSERVER_JS,
                            alert_log=self.log,
                            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

    def _new_appeared(self) -> bool:
        result = safe_execute_script(
            self.driver, CHECK_FLAG_JS, alert_log=self.log,
            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
        return bool(result)

    def _reset_flag(self):
        safe_execute_script(self.driver, RESET_FLAG_JS,
                            alert_log=self.log,
                            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

    def _block_count(self) -> int:
        result = safe_execute_script(
            self.driver, BLOCK_COUNT_JS, alert_log=self.log,
            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
        try:
            return int(result) if result is not None else -1
        except (TypeError, ValueError):
            return -1

    def _scroll_burst(self, n: int = 3):
        for _ in range(n):
            safe_execute_script(self.driver, SCROLL_JS,
                                alert_log=self.log,
                                rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
            time.sleep(self.cfg.get("scroll_pause", 0.15))
            safe_execute_script(self.driver, TRIGGER_MORE_JS,
                                alert_log=self.log,
                                rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

    def _jiggle(self):
        """Scroll to top then back to bottom — wakes up stuck lazy-loaders."""
        safe_execute_script(self.driver, JIGGLE_JS,
                            alert_log=self.log,
                            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
        time.sleep(0.15)
        safe_execute_script(self.driver, SCROLL_JS,
                            alert_log=self.log,
                            rate_limit_wait=self.cfg.get("rate_limit_wait", 300))

    # ── per-window scraping ───────────────────────────────────────────────────

    def _scrape_window(self, from_d: str, to_d: str) -> tuple[int, str]:
        """
        Returns (contracts_found, note) where note is one of:
          'normal' | 'zero_confirmed' | 'zero_assumed'
        """
        self.driver.get(SEARCH_URL)
        time.sleep(1.5)

        first_html = self.automator.auto_fill_and_submit(
            from_d, to_d, self.cfg.get("category", ""))
        if not first_html:
            raise RuntimeError(
                f"CAPTCHA/form automation failed for {from_d}→{to_d}")

        seen: set = set()
        records, new_seen = parse_html_incremental(first_html, seen)
        seen.update(new_seen)
        if records:
            self.writer.push(records)
        self.log.info(
            f"W{self.wid} {from_d}: page1 → "
            f"{len(records)} rows, {len(seen)} contracts")

        # ── 0-contract day: skip scroll loop, finish immediately ──────────────
        if len(seen) == 0:
            note = ("zero_assumed"
                    if self.automator._zero_assumed
                    else "zero_confirmed")
            self.log.info(
                f"W{self.wid} {from_d}→{to_d}: "
                f"DONE — 0 contracts (note={note})")
            return 0, note

        # ── has contracts: scroll for more ────────────────────────────────────
        self._install_observer()
        self._reset_flag()

        no_growth  = 0
        cycle      = 0
        limit      = self.cfg.get("no_growth_limit", 8)
        base_wait  = self.cfg.get("no_growth_base_wait", 1.0)
        max_wait   = self.cfg.get("no_growth_max_wait", 10.0)
        max_cycles = self.cfg.get("max_cycles", 9999)

        while cycle < max_cycles:
            cycle      += 1
            aggressive  = no_growth >= 2
            extra = (min(base_wait * (1.6 ** no_growth), max_wait)
                     if no_growth > 0 else 0.0)

            self._scroll_burst(6 if aggressive else 3)
            if aggressive:
                self._jiggle()
            if extra:
                time.sleep(extra)

            dom_count = self._block_count()
            flag_set  = self._new_appeared()

            if dom_count > len(seen) or flag_set:
                self._reset_flag()
                html = safe_get_source(
                    self.driver, alert_log=self.log,
                    rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
                records, new_seen = parse_html_incremental(html, seen)
                if records:
                    no_growth = 0
                    seen.update(new_seen)
                    self.writer.push(records)
                    self.log.info(
                        f"W{self.wid} {from_d} cycle {cycle}: "
                        f"+{len(records)} rows "
                        f"(contracts so far: {len(seen)})")
                    continue

            no_growth += 1

            if no_growth >= limit:
                self.log.info(
                    f"W{self.wid} {from_d}: no growth for {no_growth} cycles "
                    f"— final {max_wait:.0f}s patience check")
                time.sleep(max_wait)
                self._scroll_burst(6)
                self._jiggle()

                dom_count = self._block_count()
                if dom_count > len(seen) or self._new_appeared():
                    html = safe_get_source(
                        self.driver, alert_log=self.log,
                        rate_limit_wait=self.cfg.get("rate_limit_wait", 300))
                    records, new_seen = parse_html_incremental(html, seen)
                    if records:
                        seen.update(new_seen)
                        self.writer.push(records)
                        self.log.info(
                            f"W{self.wid} {from_d}: final check found "
                            f"+{len(records)} more — resuming")
                        no_growth = 0
                        continue
                break

        self.log.info(
            f"W{self.wid} {from_d}→{to_d}: "
            f"DONE — {len(seen)} contracts in {cycle} cycles")
        return len(seen), "normal"

    # ── main loop: drain the shared job queue ─────────────────────────────────

    def run(self):
        self._start()
        windows_processed = 0
        restart_every     = self.cfg.get("restart_every", 8)

        try:
            while True:
                try:
                    from_d, to_d = self.job_queue.get_nowait()
                except queue.Empty:
                    break

                if self.tracker.is_done(from_d, to_d):
                    continue  # safety net — done by another worker/run

                try:
                    n, note = self._scrape_window(from_d, to_d)
                    self.tracker.mark_done(from_d, to_d, n, note)
                except KeyboardInterrupt:
                    raise
                except (InvalidSessionIdException,
                        NoSuchWindowException,
                        WebDriverException) as e:
                    self.log.error(
                        f"W{self.wid}: browser crashed during "
                        f"{from_d}→{to_d} ({type(e).__name__}) "
                        f"— recreating browser and retrying")
                    self.job_queue.put((from_d, to_d))
                    self._quit()
                    self._start()
                    time.sleep(5)
                    continue
                except Exception as e:
                    self.log.error(
                        f"Window {from_d}→{to_d} FAILED: {e}", exc_info=True)
                    self.log.info(
                        f"W{self.wid}: will retry {from_d}→{to_d} later "
                        f"(not marked done)")
                    self.job_queue.put((from_d, to_d))
                    self._quit()
                    self._start()
                    time.sleep(5)
                    continue

                windows_processed += 1
                if restart_every and windows_processed % restart_every == 0:
                    self.log.info(
                        f"W{self.wid}: restarting browser after "
                        f"{windows_processed} windows")
                    self._quit()
                    self._start()
                else:
                    time.sleep(random.uniform(0.5, 1.5))

        except KeyboardInterrupt:
            self.log.info(f"W{self.wid} interrupted")
        finally:
            self._quit()

# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS MONITOR
# ─────────────────────────────────────────────────────────────────────────────

class ProgressMonitor(threading.Thread):
    def __init__(self, writer: DBWriter, tracker: WindowTracker,
                 total_windows: int, interval: float = 20.0):
        super().__init__(daemon=True)
        self.writer        = writer
        self.tracker       = tracker
        self.total_windows = total_windows
        self.interval      = interval
        self._stop         = threading.Event()
        self._start_t      = time.time()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            time.sleep(self.interval)
            elapsed      = time.time() - self._start_t
            total        = self.writer.total_saved
            done_windows, _ = self.tracker.summary()
            rate         = total / max(elapsed, 1) * 60
            print(
                f"\n  ── PROGRESS ──  "
                f"rows saved: {total:,}  |  "
                f"windows done: {done_windows}/{self.total_windows}  |  "
                f"rate: {rate:.1f} rows/min  |  "
                f"elapsed: {elapsed/60:.1f} min\n"
            )

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(db_path: str, tracker: WindowTracker, total_windows: int):
    try:
        conn  = sqlite3.connect(db_path)
        total = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        done_windows, _ = tracker.summary()
        print("\n" + "═" * 65)
        print("  FINAL SUMMARY")
        print("═" * 65)
        print(f"  Total contracts        : {total:,}")
        print(f"  Date-windows completed : {done_windows}/{total_windows}")

        zero_assumed = tracker.zero_assumed_dates()
        if zero_assumed:
            print(
                f"\n  ⚠️  {len(zero_assumed)} window(s) assumed 0 contracts "
                f"(no explicit confirmation) — spot-check if you like:")
            for fd, td in zero_assumed[:15]:
                print(f"      {fd} → {td}")
            if len(zero_assumed) > 15:
                print(f"      ... and {len(zero_assumed)-15} more")
            print(f"  To re-check one:  --from <date> --to <date> "
                  f"--limit-windows 1")
            print(f"  (delete the row from completed_windows first "
                  f"so it re-queues)")

        rows = conn.execute("""
            SELECT ministry, COUNT(*) n, SUM(total_value) v
            FROM contracts WHERE ministry != ''
            GROUP BY ministry ORDER BY n DESC LIMIT 10
        """).fetchall()
        print("\n  Top 10 Ministries:")
        for r in rows:
            print(f"    {r[0][:45]:<45}  {r[1]:>5}  "
                  f"₹{(r[2] or 0):>15,.0f}")
        row = conn.execute("""
            SELECT MIN(total_value), MAX(total_value), AVG(total_value)
            FROM contracts WHERE total_value > 0
        """).fetchone()
        if row and row[0]:
            print(
                f"\n  Price — Min: ₹{row[0]:,.2f}  "
                f"Max: ₹{row[1]:,.2f}  Avg: ₹{row[2]:,.2f}")
        conn.close()
        if done_windows < total_windows:
            print(f"\n  {total_windows - done_windows} window(s) still pending.")
            print(f"  Run the SAME command again to resume — "
                  f"completed days are skipped.")
        print("═" * 65)
    except Exception as e:
        print(f"Summary error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="GEM Contract Scraper v3.2 — Smarter CAPTCHA/Result Handling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python gem_scraper_auto_v3.2.py --from 01-01-2024 --to 31-12-2024 --workers 4
  python gem_scraper_auto_v3.2.py --from 01-01-2025 --to 31-01-2025 --workers 2
  python gem_scraper_auto_v3.2.py --from 01-06-2026 --to 12-06-2026 --limit-windows 3
  python gem_scraper_auto_v3.2.py --from 01-03-2024 --to 31-03-2024 --workers 3 \\
      --headless --no-growth 10 --no-growth-max-wait 15 \\
      --page-load-wait 8 --ambiguous-threshold 5

  # Interrupted? Just re-run the SAME command — finished days are skipped.
        """
    )
    parser.add_argument("--from", dest="from_date",
                        default=DEFAULT_CONFIG["from_date"],
                        metavar="DD-MM-YYYY")
    parser.add_argument("--to", dest="to_date",
                        default=DEFAULT_CONFIG["to_date"],
                        metavar="DD-MM-YYYY")
    parser.add_argument("--step-days", type=int,
                        default=DEFAULT_CONFIG["step_days"],
                        dest="step_days", metavar="N")
    parser.add_argument("--workers", type=int, default=1, metavar="N")
    parser.add_argument("--db", dest="db_path",
                        default=DEFAULT_CONFIG["db_path"])
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-growth", type=int,
                        default=DEFAULT_CONFIG["no_growth_limit"],
                        dest="no_growth", metavar="N")
    parser.add_argument("--no-growth-max-wait", type=float,
                        default=DEFAULT_CONFIG["no_growth_max_wait"],
                        dest="no_growth_max_wait", metavar="SEC")
    parser.add_argument(
        "--page-load-wait", type=float,
        default=DEFAULT_CONFIG["page_load_wait"],
        dest="page_load_wait", metavar="SEC",
        help="max seconds each poll cycle waits for a page signal "
             "(default: 8 — was 50 in v3.1)")
    parser.add_argument(
        "--ambiguous-threshold", type=int,
        default=DEFAULT_CONFIG["ambiguous_threshold"],
        dest="ambiguous_threshold", metavar="N",
        help="poll-loop timeouts with no signal before assuming 0 contracts "
             "(default: 5)")
    parser.add_argument("--restart-every", type=int,
                        default=DEFAULT_CONFIG["restart_every"],
                        dest="restart_every", metavar="N")
    parser.add_argument("--limit-windows", type=int, default=0,
                        dest="limit_windows", metavar="N")
    parser.add_argument("--max-captcha-tries", type=int,
                        default=DEFAULT_CONFIG["max_captcha_tries"],
                        dest="max_captcha_tries", metavar="N")
    parser.add_argument("--captcha-wait", type=float,
                        default=DEFAULT_CONFIG["captcha_wait"],
                        dest="captcha_wait", metavar="SEC")
    parser.add_argument("--category", default="", metavar="CAT")
    args = parser.parse_args()

    config = {
        **DEFAULT_CONFIG,
        "headless":           args.headless,
        "db_path":            args.db_path,
        "step_days":          args.step_days,
        "no_growth_limit":    args.no_growth,
        "no_growth_max_wait": args.no_growth_max_wait,
        "page_load_wait":     args.page_load_wait,
        "ambiguous_threshold":args.ambiguous_threshold,
        "restart_every":      args.restart_every,
        "max_captcha_tries":  args.max_captcha_tries,
        "captcha_wait":       args.captcha_wait,
        "category":           args.category,
    }

    all_windows = generate_windows(
        args.from_date, args.to_date, args.step_days)
    tracker = WindowTracker(config["db_path"])

    pending      = [w for w in all_windows if not tracker.is_done(*w)]
    already_done = len(all_windows) - len(pending)

    if args.limit_windows > 0:
        pending = pending[:args.limit_windows]

    print()
    print("═" * 65)
    print("  GEM CONTRACT SCRAPER  v3.2  —  SMARTER CAPTCHA/RESULT HANDLING")
    print("═" * 65)
    print(f"  Date range       : {args.from_date}  →  {args.to_date}")
    print(f"  Step size        : {args.step_days} day(s)  "
          f"→  {len(all_windows)} total windows")
    print(f"  Already done     : {already_done}  (resumed from previous run)")
    print(f"  Pending now      : {len(pending)}")
    print(f"  Workers          : {args.workers}")
    print(f"  no_growth limit  : {config['no_growth_limit']}  "
          f"(escalating wait up to {config['no_growth_max_wait']:.0f}s)")
    print(f"  page_load_wait   : {config['page_load_wait']:.0f}s  "
          f"(was 50s in v3.1)")
    print(f"  ambiguous limit  : {config['ambiguous_threshold']}  "
          f"(0-contract days resolve in "
          f"~{config['ambiguous_threshold'] * (config['page_load_wait'] + 1):.0f}s "
          f"worst case)")
    print(f"  Restart every    : {config['restart_every']} window(s)")
    print(f"  Database         : {config['db_path']}")
    print(f"  Headless         : {args.headless}")
    print("═" * 65)

    if not pending:
        print("\n  Nothing to do — all requested windows are already completed.")
        print("  Widen --from/--to to scrape more dates.\n")
        return

    print(f"\n  First window : {pending[0][0]} → {pending[0][1]}")
    print(f"  Last window  : {pending[-1][0]} → {pending[-1][1]}\n")
    print("  Initialising CAPTCHA solver...")

    try:
        captcha_solver = CaptchaSolver()
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    job_queue: "queue.Queue[tuple[str,str]]" = queue.Queue()
    for w in pending:
        job_queue.put(w)

    db_writer = DBWriter(db_path=config["db_path"], config=config)
    db_writer.start()

    monitor = ProgressMonitor(
        db_writer, tracker,
        total_windows=len(all_windows), interval=20.0)
    monitor.start()

    threads = []
    for i in range(args.workers):
        worker = ScrapingWorker(
            worker_id     = str(i + 1),
            job_queue     = job_queue,
            tracker       = tracker,
            config        = config,
            db_writer     = db_writer,
            captcha_solver= captcha_solver,
        )
        t = threading.Thread(
            target=worker.run, name=f"Worker-{i+1}", daemon=False)
        threads.append(t)
        t.start()
        time.sleep(2.0)  # stagger browser launches

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("Interrupted — finishing current windows, then stopping...")
        for t in threads:
            t.join(timeout=10)

    monitor.stop()
    db_writer.stop()
    print_summary(config["db_path"], tracker, len(all_windows))
    log.info(f"Complete. Total rows saved this run: {db_writer.total_saved:,}")


if __name__ == "__main__":
    main()