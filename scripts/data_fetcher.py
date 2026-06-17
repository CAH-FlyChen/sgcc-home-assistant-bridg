import logging
import os
import re
import time
import json
import threading

import random
import base64
from datetime import datetime
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common.exceptions import TimeoutException
from sensor_updator import SensorUpdator
from error_watcher import ErrorWatcher
from typing import Optional
from model import AccountData, FetchRun, SessionCheck, mask_account_no
from scraper import Scraper, redact_account_data
from store import Store

from const import *

from captcha_selenium import solve_captcha_in_browser
_FETCH_LOCK = threading.Lock()

class _AccountNoRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
            record.msg = re.sub(
                r"(?<!\d)(\d{13})(?!\d)",
                lambda m: mask_account_no(m.group(1)),
                message,
            )
            record.args = ()
        except Exception:
            pass
        return True

_ACCOUNT_REDACTION_FILTER = _AccountNoRedactionFilter()

def _install_account_log_redaction() -> None:
    root = logging.getLogger()
    if _ACCOUNT_REDACTION_FILTER not in root.filters:
        root.addFilter(_ACCOUNT_REDACTION_FILTER)
    for handler in root.handlers:
        if _ACCOUNT_REDACTION_FILTER not in handler.filters:
            handler.addFilter(_ACCOUNT_REDACTION_FILTER)


class DataFetcher:

    def __init__(self, username: str, password: str):
        if 'PYTHON_IN_DOCKER' not in os.environ:
            import dotenv
            dotenv.load_dotenv(verbose=True)
        self._username = username
        self._password = password

        self.DRIVER_IMPLICITY_WAIT_TIME = int(os.getenv("DRIVER_IMPLICITY_WAIT_TIME", 60))
        self.RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
        self.LOGIN_EXPECTED_TIME = int(os.getenv("LOGIN_EXPECTED_TIME", 10))
        self.RETRY_WAIT_TIME_OFFSET_UNIT = int(os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", 10))
        self.IGNORE_USER_ID = [uid.strip() for uid in os.getenv("IGNORE_USER_ID", "").split(",") if uid.strip()]
        self.PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", 45))
        self.QR_CODE_LOGIN_WAIT_COUNT = int(os.getenv("QR_CODE_LOGIN_WAIT_COUNT", 7))
        self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT = int(os.getenv("QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT", 10))
        self._user_name_map = {}
        raw_names = os.getenv("USER_NAMES", "")
        if raw_names:
            for pair in raw_names.split(","):
                if ":" in pair:
                    uid, name = pair.split(":", 1)
                    self._user_name_map[uid.strip()] = name.strip()
    @staticmethod
    def _mask_secret(value: str, keep_last: int = 2) -> str:
        if not value:
            return ""
        value = str(value)
        if len(value) <= keep_last:
            return "*" * len(value)
        return "*" * (len(value) - keep_last) + value[-keep_last:]

    def _safe_get(self, driver, url: str, label: str = "页面", fast: bool = False):
        """Navigate with a bounded page-load timeout.

        95598 pages may keep long-polling or hold subresources open. Selenium's
        default get() waits for full document load and can block the whole fetch
        job. For post-login SPA pages, use JS navigation and stop loading after
        the route/DOM becomes observable.
        """
        logging.info(f"正在打开{label}: {url}")
        if fast:
            old_wait = self.DRIVER_IMPLICITY_WAIT_TIME
            try:
                driver.implicitly_wait(0)
                driver.execute_script("window.location.href = arguments[0];", url)
                deadline = int(os.getenv("FAST_NAV_WAIT", 20))
                WebDriverWait(driver, deadline).until(
                    lambda d: url.split('/osgweb')[-1] in (d.current_url or '')
                    or (d.execute_script("return document.readyState") in ("interactive", "complete"))
                )
            except TimeoutException as e:
                logging.warning(f"快速打开{label}等待超时，执行 window.stop() 后继续: {e}")
            except Exception as e:
                logging.warning(f"快速打开{label}异常，继续使用当前页面: {e}")
            finally:
                try:
                    driver.execute_script("window.stop();")
                except Exception as stop_error:
                    logging.warning(f"{label} window.stop() 失败: {stop_error}")
                driver.implicitly_wait(old_wait)
            return

        try:
            driver.get(url)
        except TimeoutException as e:
            logging.warning(f"打开{label}超时({self.PAGE_LOAD_TIMEOUT}s)，执行 window.stop() 后继续: {e}")
            try:
                driver.execute_script("window.stop();")
            except Exception as stop_error:
                logging.warning(f"{label} window.stop() 失败: {stop_error}")
        except Exception as e:
            logging.warning(f"打开{label}异常，继续使用当前页面: {e}")

    # @staticmethod
    def _click_button(self, driver, button_search_type, button_search_key):
        '''封装点击函数，仅在元素可点击时点击'''
        click_element = driver.find_element(button_search_type, button_search_key)
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.element_to_be_clickable(click_element))
        driver.execute_script("arguments[0].click();", click_element)
        # 点击后添加微小随机暂停，模拟人工操作
        time.sleep(random.uniform(0.1, 0.5))


    def _get_webdriver(self):
        chrome_options = webdriver.ChromeOptions()

        # SGCC_REAL_BROWSER=1 + Docker 时是 attach 到 start-real-browser.sh 已启动的持久 Chromium。
        # 这种模式下 ChromeDriver 只应携带 debuggerAddress；excludeSwitches / prefs 等启动选项
        # 不能应用到已存在浏览器，会导致 invalid argument: unrecognized chrome option。
        real_browser = os.getenv("SGCC_REAL_BROWSER", "false").lower() in ("1", "true", "yes", "on")
        attach_existing_browser = real_browser and ('PYTHON_IN_DOCKER' in os.environ)

        # 可选：环境变量自定义反检测参数
        browser_lang = os.getenv("BROWSER_LANGUAGE", "zh-HK,zh,en-US,en")
        browser_ua = os.getenv("BROWSER_USER_AGENT", "")
        device_scale = os.getenv("BROWSER_DEVICE_SCALE_FACTOR", "2")
        window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")

        if not attach_existing_browser:
            # 基础参数
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--start-maximized")

            # 反检测核心参数（参考 ha-95598）
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option("useAutomationExtension", False)

            chrome_options.add_argument(f"--lang={browser_lang}")
            chrome_options.add_argument(f"--window-size={window_size}")
            chrome_options.add_argument(f"--force-device-scale-factor={device_scale}")
            chrome_options.add_argument("--high-dpi-support=1")
            if browser_ua:
                chrome_options.add_argument(f"user-agent={browser_ua}")

            chrome_options.add_experimental_option("prefs", {
                "intl.accept_languages": browser_lang,
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            })

        # Docker 环境默认 headless；SGCC_REAL_BROWSER=1 时连接 start-real-browser.sh 启动的持久 Chromium。
        if 'PYTHON_IN_DOCKER' in os.environ:
            chrome_options.binary_location = "/usr/bin/chromium"
            service = ChromeService(executable_path="/usr/bin/chromedriver")
            if real_browser:
                debugger_address = os.getenv("BROWSER_CDP_ADDRESS", "127.0.0.1:9222")
                chrome_options.debugger_address = debugger_address
                logging.info(f"使用持久真实浏览器会话: {debugger_address}")
            else:
                chrome_options.add_argument("--headless=new")

            def _setting_driver(driver):
                # 显式设置窗口大小（解决无头模式下 --window-size 不生效的问题）
                width, height = map(int, window_size.split(','))
                try:
                    driver.set_window_size(width, height)
                except Exception as e:
                    logging.warning(f"设置窗口大小失败: {e}")
                try:
                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": int(device_scale),
                        "mobile": False,
                        "dontSetVisibleSize": False
                    })
                except Exception as e:
                    logging.warning(f"CDP 设置 viewport 失败: {e}")
                
        else:
            service = self._find_chromedriver()
            if real_browser:
                profile_dir = os.getenv("SGCC_BROWSER_PROFILE", os.path.expanduser("~/.local/share/sgcc-electricity-arc/chrome-profile"))
                os.makedirs(profile_dir, exist_ok=True)
                chrome_options.add_argument(f"--user-data-dir={profile_dir}")
                logging.info(f"使用本机持久浏览器 profile: {profile_dir}")
            def _setting_driver(driver):
                driver.maximize_window()

        driver = webdriver.Chrome(options=chrome_options, service=service)
        driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)
        driver.set_page_load_timeout(self.PAGE_LOAD_TIMEOUT)
        
        _setting_driver(driver)
        
        return driver

    @staticmethod
    def _find_chromedriver() -> ChromeService:
        """在非 Docker 环境中查找可用的 ChromeDriver。"""
        import shutil

        # 1) 尝试系统 PATH
        path = shutil.which("chromedriver") or shutil.which("chromedriver.exe")
        if path:
            return ChromeService(executable_path=path)

        # 2) 尝试 CloakBrowser 缓存的 chromedriver（如果有）
        for base in [
            os.path.expanduser("~/.cloakbrowser"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), ".cloakbrowser"),
        ]:
            try:
                for root, dirs, files in os.walk(base):
                    if "chromedriver.exe" in files or "chromedriver" in files:
                        fname = "chromedriver.exe" if "chromedriver.exe" in files else "chromedriver"
                        path = os.path.join(root, fname)
                        if os.path.isfile(path):
                            return ChromeService(executable_path=path)
                    # 最多扫描两级目录
                    if len(root) - len(base) > 200:
                        dirs.clear()
            except Exception:
                pass

        # 3) 尝试 Selenium Manager 自动下载
        try:
            return ChromeService()
        except Exception:
            pass

        raise RuntimeError(
            "ChromeDriver 未找到。请安装 ChromeDriver 或运行: pip install chromedriver-binary-auto"
        )

    @staticmethod
    def _is_logged_in_page(driver) -> bool:
        try:
            current_url = driver.current_url or ""
            if "/osgweb/login" not in current_url and "/osgweb/" in current_url:
                return True
            return bool(driver.execute_script("""
                return !!(
                    document.querySelector('.el-dropdown') ||
                    document.querySelector('.userName') ||
                    document.body.innerText.includes('我的') ||
                    document.body.innerText.includes('安全退出')
                );
            """))
        except Exception:
            return False

    @ErrorWatcher.watch
    def _login(self, driver, phone_code = False):
        if os.getenv("SGCC_REAL_BROWSER", "false").lower() in ("1", "true", "yes", "on"):
            try:
                driver.execute_script("return document.readyState")
                if self._is_logged_in_page(driver):
                    logging.info(f"检测到持久浏览器已有登录态: {driver.current_url}")
                    return True
            except Exception as e:
                logging.warning(f"检查持久浏览器登录态失败，将打开登录页: {e}")
        try:
            self._safe_get(driver, LOGIN_URL, "登录页面")
            if self._is_logged_in_page(driver):
                logging.info(f"打开登录页后检测到已登录态: {driver.current_url}")
                return True
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME * 3).until(EC.visibility_of_element_located((By.CLASS_NAME, "user")))
        except Exception:
            logging.error(f"登录页面加载失败: {LOGIN_URL}")
            return False
        logging.info(f"打开登录页面: {LOGIN_URL}。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
        # swtich to username-password login page
        # 临时关闭隐式等待，避免与 WebDriverWait 叠加导致超时
        driver.implicitly_wait(0)
        try:
            WebDriverWait(driver, 10).until(
                EC.invisibility_of_element_located((By.CLASS_NAME, 'el-loading-mask')))
        finally:
            driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

        element = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'user')))
        driver.execute_script("arguments[0].click();", element)
        logging.info("已找到 'user' 元素。\r")
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 点击同意按钮
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]')
        logging.info("已点击同意选项。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        if phone_code:
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[2].send_keys(self._username)
            logging.info(f"已输入用户名: {self._mask_secret(self._username)}\r")
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[1]/div[2]/div[2]/div/a')
            code = input("请输入手机验证码: ")
            input_elements[3].send_keys(code)
            logging.info(f"已输入验证码: {code}。\r")
            # 点击登录按钮
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span')
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
            logging.info("已点击登录按钮。\r")

            return True
        # 增加判空校验便于测试备用方案
        elif self._password is not None and len(self._password) > 0:
            # 输入用户名和密码
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[0].send_keys(self._username)
            logging.info(f"已输入用户名: {self._mask_secret(self._username)}\r")
            input_elements[1].send_keys(self._password)
            logging.info("已输入密码: ***MASKED***\r")

            # 点击登录按钮
            self._click_button(driver, By.CLASS_NAME, "el-button.el-button--primary")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("已点击登录按钮。\r")

            # 快速检查：如果已经跳转离开登录页，说明无需验证码，直接成功
            if driver.current_url != LOGIN_URL:
                logging.info("无需验证码登录成功 (已被重定向)。\r")
                return True

            # 会出现点击登录直接失败（账号被限制登录）
            error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
            if error is None:
                # 处理腾讯点击验证码
                captcha_passed = solve_captcha_in_browser(driver, max_retries=self.RETRY_TIMES_LIMIT)
                if captcha_passed:
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    if driver.current_url != LOGIN_URL:
                        logging.info("通过点击验证码登录成功。\r")
                        return True
                    else:
                        error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                        if error:
                            logging.info(f"验证码通过但登录失败: [{error}]\r")
                        else:
                            logging.error("验证码已通过但仍停留在登录页面。")
                else:
                    error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                    logging.error("点击验证码识别在所有重试后均失败。")
            else:
                logging.error(f"登录失败: [{error}]\r")    
        return self._fallback_login(driver, error)

    def _get_error_message(self, driver, path) -> Optional[str]:
        """获取错误信息，如果不存在则返回 None"""
        # 关闭隐式等待
        driver.implicitly_wait(0)
        try:
            element = driver.find_element(By.XPATH, path)
            return element.text
        except Exception:
            return None
        finally:
            driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

    def _fallback_login(self, driver, reason: str) -> bool:
        """使用备用方案登录"""
        fallback = os.getenv("LOGIN_FALLBACK")
        if fallback == 'qrcode':
            return self._qr_login(driver, reason)
        return False

    def _qr_login(self, driver, reason: str) -> bool:
        logging.info("二维码登录开始")
        # 切换验证码
        element = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'qr_code')))
        driver.execute_script("arguments[0].click();", element)
        logging.info("已切换到二维码模式")

        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 获取登录二维码
        qrElement = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.visibility_of_element_located((By.XPATH, "//div[@class='sweepCodePic']//img")))
        logging.info("已找到二维码图片元素")

        img_src = qrElement.get_attribute('src')

        if img_src.startswith('data:image'):
            base64_data = img_src.split(',')[1]
            img_screenshot = base64.b64decode(base64_data)
        else:
          logging.info('二维码图片源不是 base64 格式')
          img_screenshot = qrElement.screenshot_as_png

        with open("/data/login_qr_code.png", "wb") as f:
            f.write(img_screenshot)
            logging.info("已将二维码保存到 /data/login_qr_code.png")

        from notify import UrlLoginQrCodeNotify
        notifyFunc = UrlLoginQrCodeNotify()
        notifyFunc(img_screenshot, reason)
        for i in range(1, self.QR_CODE_LOGIN_WAIT_COUNT + 1):
            logging.info(f'二维码登录等待检查[{self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT}] 次数[{i}]')
            time.sleep(self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT)
            if (driver.current_url != LOGIN_URL):
                logging.info("二维码登录成功")
                return True
            else:
                error = self._get_error_message(driver, "//div[@class='sweepCodePic']//div[@class='erwBg']//p")
                if error is not None:
                    logging.error(f'二维码登录错误[{error}]')
                    return False

        logging.warning("二维码登录超时")

        return False

    def _random_delay(self, min_seconds=0.5, max_seconds=3.0):
        """添加随机延迟，使自动化操作更难被检测。"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)


    def fetch(self, trigger_type: str = "manual"):

        """主逻辑：登录链路保持原样，数据抓取切换到 Path B + Store。"""

        _install_account_log_redaction()

        if not _FETCH_LOCK.acquire(blocking=False):
            self._record_skipped_busy_run(trigger_type)
            logging.info("已有抓取任务正在运行，本次 fetch 标记为 skipped_busy 后跳过。")
            return "skipped_busy"

        driver = None
        store = None
        run_id = None
        session_status_before = "unknown"
        session_status_after = "unknown"
        try:
            store = Store()
            run_id = store.start_run(FetchRun(
                trigger_type=trigger_type,
                started_at=self._now_iso(),
            ))

            driver = self._get_webdriver()
            ErrorWatcher.instance().set_driver(driver)

            self._random_delay(1, 3)
            logging.info("浏览器驱动已初始化。")
            updator = SensorUpdator()

            before_check = self._session_check(driver, "before_login")
            session_status_before = before_check.status
            store.record_session_check(before_check)

            try:
                if os.getenv("DEBUG_MODE", "false").lower() == "true":
                    if self._login(driver, phone_code=True):
                        logging.info("登录成功!")
                    else:
                        logging.info("登录失败!")
                        raise Exception("login unsuccessed")
                else:
                    if self._login(driver):
                        logging.info("登录成功!")
                    else:
                        logging.info("登录失败!")
                        raise Exception("login unsuccessed")
            except Exception as e:
                logging.error(
                    f"浏览器驱动异常，原因: {self._redact_text(e)}。还剩 {self.RETRY_TIMES_LIMIT} 次重试机会。")
                raise

            logging.info(f"在 {LOGIN_URL} 登录成功")
            after_login_check = self._session_check(driver, "after_login")
            store.record_session_check(after_login_check)
            session_status_after = after_login_check.status

            if after_login_check.status != "authenticated":
                raise Exception(f"session not authenticated after login: {after_login_check.status}")

            self._random_delay(1, 3)
            logging.info("开始使用 Path B 从 Vue/Vuex 状态抓取账户数据。")
            account_data_list = Scraper(driver).fetch_all()
            if not account_data_list:
                raise Exception("Path B 未抓取到任何账户数据")

            saved_count = 0
            for account_data in account_data_list:
                user_id = account_data.account.account_no
                masked_user_id = mask_account_no(user_id)
                if not user_id:
                    logging.warning("Path B 返回了缺少户号的账户数据，已跳过。")
                    continue
                if user_id in self.IGNORE_USER_ID:
                    logging.info(f"用户 ID {masked_user_id} 将被忽略")
                    continue

                store.save_account_data(account_data, run_id)
                saved_count += 1
                logging.info(f"用户 [{masked_user_id}] Path B 数据已写入 Store: {self._account_data_summary(account_data)}")
                logging.debug(f"用户 [{masked_user_id}] Path B 脱敏数据: {redact_account_data(account_data)}")

                update_args = self._account_data_to_update_args(account_data)
                logging.info(
                    f"用户 [{masked_user_id}] 数据获取完成: 余额={update_args['balance']}元, "
                    f"最近日用电={update_args['last_daily_usage']}度({update_args['last_daily_date']}), "
                    f"年度用电={update_args['yearly_usage']}度, 年度电费={update_args['yearly_charge']}元, "
                    f"月用电={update_args['month_usage']}度, 月电费={update_args['month_charge']}元")
                updator.update_one_userid(**update_args)

            if saved_count == 0:
                raise Exception("Path B 抓取结果均为空或被忽略，未写入任何账户数据")

            final_check = self._session_check(driver, "after_fetch")
            store.record_session_check(final_check)
            session_status_after = final_check.status
            store.finish_run(
                run_id,
                "success",
                session_status_before=session_status_before,
                session_status_after=session_status_after,
            )
            logging.info(f"抓取运行 {run_id} 完成: success, 账户数={saved_count}, 会话={session_status_after}")
            return "success"
        except Exception as e:
            if driver is not None and store is not None:
                try:
                    failed_check = self._session_check(driver, "failed")
                    store.record_session_check(failed_check)
                    session_status_after = failed_check.status
                except Exception:
                    pass
            if store is not None and run_id is not None:
                try:
                    store.finish_run(
                        run_id,
                        "failed",
                        session_status_before=session_status_before,
                        session_status_after=session_status_after,
                        error_type=type(e).__name__,
                        error_message_redacted=self._redact_text(e),
                    )
                except Exception as finish_error:
                    logging.warning(f"记录 fetch run 失败状态失败: {self._redact_text(finish_error)}")
            raise
        finally:
            if driver is not None:
                self._release_driver(driver)
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            _FETCH_LOCK.release()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _redact_text(value) -> str:
        text = str(value)
        return re.sub(r"(?<!\d)(\d{13})(?!\d)", lambda m: mask_account_no(m.group(1)), text)

    @staticmethod
    def _redact_url(url: str) -> str:
        if not url:
            return ""
        return re.sub(r"(?<!\d)(\d{13})(?!\d)", lambda m: mask_account_no(m.group(1)), url.split("?", 1)[0])

    @staticmethod
    def _attach_existing_browser_enabled() -> bool:
        real_browser = os.getenv("SGCC_REAL_BROWSER", "false").lower() in ("1", "true", "yes", "on")
        return real_browser and ('PYTHON_IN_DOCKER' in os.environ)

    def _release_driver(self, driver) -> None:
        if self._attach_existing_browser_enabled():
            logging.info("当前为持久 Chromium attach 模式，跳过 driver.quit()，保留登录会话。")
            return
        try:
            driver.quit()
            logging.info("数据抓取完成后浏览器驱动退出。")
        except Exception as e:
            logging.warning(f"浏览器驱动退出失败: {self._redact_text(e)}")

    def _record_skipped_busy_run(self, trigger_type: str) -> None:
        try:
            with Store() as store:
                now = self._now_iso()
                store.start_run(FetchRun(
                    trigger_type=trigger_type,
                    status="skipped_busy",
                    started_at=now,
                    finished_at=now,
                    session_status_before="unknown",
                    session_status_after="unknown",
                ))
        except Exception as e:
            logging.warning(f"记录 skipped_busy fetch run 失败: {self._redact_text(e)}")

    def _session_check(self, driver, check_method: str) -> SessionCheck:
        current_url = ""
        try:
            current_url = driver.current_url or ""
        except Exception:
            current_url = ""
        redirected_to_login = "/osgweb/login" in current_url
        try:
            authenticated = self._is_logged_in_page(driver)
        except Exception:
            authenticated = False
        if authenticated:
            status = "authenticated"
        elif redirected_to_login:
            status = "expired"
        else:
            status = "unknown"
        safe_url = self._redact_url(current_url)
        return SessionCheck(
            checked_at=self._now_iso(),
            status=status,
            current_url=safe_url,
            check_method=check_method,
            redirected_to_login=redirected_to_login,
            evidence_redacted=f"url={safe_url}",
        )

    @staticmethod
    def _latest_daily(account_data: AccountData):
        rows = [row for row in account_data.daily if row.date]
        return max(rows, key=lambda row: row.date) if rows else None

    @staticmethod
    def _latest_monthly(account_data: AccountData):
        rows = [row for row in account_data.monthly if row.year_month]
        return max(rows, key=lambda row: row.year_month) if rows else None

    @staticmethod
    def _account_data_summary(account_data: AccountData) -> str:
        return (
            f"balance={'yes' if account_data.balance else 'no'}, "
            f"daily={len(account_data.daily)}, monthly={len(account_data.monthly)}, "
            f"yearly={'yes' if account_data.yearly else 'no'}"
        )

    def _account_data_to_update_args(self, account_data: AccountData) -> dict:
        user_id = account_data.account.account_no
        balance_model = account_data.balance
        yearly = account_data.yearly
        latest_month = self._latest_monthly(account_data)
        latest_day = self._latest_daily(account_data)

        balance = None
        enhanced_balance = None
        if balance_model is not None:
            balance = balance_model.balance_cny
            if balance is None:
                balance = balance_model.prepay_balance_cny
            if balance_model.arrears_cny is not None:
                enhanced_balance = {
                    "as_of": balance_model.observed_at,
                    "amount_due": balance_model.arrears_cny,
                    "user_id": user_id,
                }

        tou_daily = []
        for row in account_data.daily:
            tou_daily.append({
                "date": row.date,
                "total_usage": row.total_usage_kwh,
                "valley_usage": row.valley_usage_kwh,
                "flat_usage": row.flat_usage_kwh,
                "peak_usage": row.peak_usage_kwh,
                "tip_usage": row.tip_usage_kwh,
            })
        tou_data = {
            "year": yearly.year if yearly else "",
            "yearly_usage": yearly.total_usage_kwh if yearly else None,
            "yearly_charge": yearly.total_charge_cny if yearly else None,
            "months": [
                {
                    "month": row.year_month,
                    "usage": row.total_usage_kwh,
                    "charge": row.total_charge_cny,
                    "begin_date": row.begin_date,
                    "end_date": row.end_date,
                }
                for row in account_data.monthly
            ],
            "daily": tou_daily,
        } if tou_daily else None

        return {
            "user_id": user_id,
            "balance": balance,
            "last_daily_date": latest_day.date if latest_day else None,
            "last_daily_usage": latest_day.total_usage_kwh if latest_day else None,
            "yearly_charge": yearly.total_charge_cny if yearly else None,
            "yearly_usage": yearly.total_usage_kwh if yearly else None,
            "month_charge": latest_month.total_charge_cny if latest_month else None,
            "month_usage": latest_month.total_usage_kwh if latest_month else None,
            "tou_data": tou_data,
            "enhanced_balance": enhanced_balance,
        }
