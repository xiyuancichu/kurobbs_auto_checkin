import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from loguru import logger
from pydantic import BaseModel, Field

from ext_notification import NotificationService
from logging_utils import configure_logger
from settings import Settings, SettingsError, parse_bool


class Response(BaseModel):
    code: int = Field(..., alias="code", description="返回值")
    msg: str = Field(..., alias="msg", description="提示信息")
    success: Optional[bool] = Field(None, alias="success", description="token有时才有")
    data: Optional[Any] = Field(None, alias="data", description="请求成功才有")


class KurobbsClientException(Exception):
    """Custom exception for Kurobbs client errors."""


class KurobbsClient:
    FIND_ROLE_LIST_API_URL = "https://api.kurobbs.com/gamer/role/default"
    SIGN_URL = "https://api.kurobbs.com/encourage/signIn/v2"
    USER_SIGN_URL = "https://api.kurobbs.com/user/signIn"
    USER_MINE_URL = "https://api.kurobbs.com/user/mineV2"

    def __init__(self, token: str):
        if not token:
            raise KurobbsClientException("TOKEN is required to call Kurobbs APIs.")

        self.token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "osversion": "Android",
                "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
                "countrycode": "CN",
                "ip": "10.0.2.233",
                "model": "2211133C",
                "source": "android",
                "lang": "zh-Hans",
                "version": "1.0.9",
                "versioncode": "1090",
                "token": self.token,
                "content-type": "application/x-www-form-urlencoded; charset=utf-8",
                "accept-encoding": "gzip",
                "user-agent": "okhttp/3.10.0",
            }
        )
        self.result: Dict[str, str] = {}
        self.exceptions: List[Exception] = []

    def _post(self, url: str, data: Dict[str, Any]) -> Response:
        """Make a POST request to the specified URL with the given data."""
        try:
            response = self.session.post(url, data=data, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise KurobbsClientException(f"Request to {url} failed: {exc}") from exc

        try:
            res = Response.model_validate_json(response.content)
        except Exception as exc:  # noqa: BLE001
            raise KurobbsClientException(f"Failed to parse response from {url}") from exc

        logger.debug(
            "POST {} -> code={}, success={}, msg={}",
            url,
            res.code,
            res.success,
            res.msg,
        )
        return res

    def get_mine_info(self, type: int = 1) -> Dict[str, Any]:
        """Get mine info."""
        res = self._post(self.USER_MINE_URL, {"type": type})
        if not res.data:
            raise KurobbsClientException("User info is missing in response.")
        return res.data

    def get_user_game_list(self, user_id: int) -> Dict[str, Any]:
        """Get the list of games for the user."""
        res = self._post(self.FIND_ROLE_LIST_API_URL, {"queryUserId": user_id})
        if not res.data:
            raise KurobbsClientException("User game list is missing in response.")
        return res.data

    def checkin(self) -> Response:
        """Perform the check-in operation."""
        mine_info = self.get_mine_info()
        user_game_list = self.get_user_game_list(user_id=mine_info.get("mine", {}).get("userId", 0))

        beijing_tz = ZoneShiftInfo("Asia/Shanghai")
        beijing_time = datetime.now(beijing_tz)

        role_list = user_game_list.get("defaultRoleList") or []
        if not role_list:
            raise KurobbsClientException("No default role found for the user.")
        role_info = role_list[0]

        data = {
            "gameId": role_info.get("gameId", 2),
            "serverId": role_info.get("serverId"),
            "roleId": role_info.get("roleId", 0),
            "userId": role_info.get("userId", 0),
            "reqMonth": f"{beijing_time.month:02d}",
        }
        return self._post(self.SIGN_URL, data)

    def sign_in(self) -> Response:
        """Perform the sign-in operation."""
        return self._post(self.USER_SIGN_URL, {"gameId": 2})

    def _process_sign_action(
        self,
        action_name: str,
        action_method: Callable[[], Response],
        success_message: str,
        failure_message: str,
    ):
        """Handle the common logic for sign-in actions."""
        resp = action_method()
        if resp.success:
            self.result[action_name] = success_message
            logger.info("{} -> {}", action_name, success_message)
        else:
            self.exceptions.append(KurobbsClientException(f"{failure_message}, {resp.msg}"))

    def start(self):
        """Start the sign-in process."""
        logger.info(f"开始处理账号: {self.token[:10]}...") # 打印token前10位用于区分
        self._process_sign_action(
            action_name="checkin",
            action_method=self.checkin,
            success_message="签到奖励签到成功",
            failure_message="签到奖励签到失败",
        )

        self._process_sign_action(
            action_name="sign_in",
            action_method=self.sign_in,
            success_message="社区签到成功",
            failure_message="社区签到失败",
        )

        self._log()

    @property
    def msg(self) -> str:
        return ", ".join(self.result.values()) + "!" if self.result else ""

    def _log(self):
        """Log the results and raise exceptions if any."""
        if msg := self.msg:
            logger.info(msg)
        if self.exceptions:
            raise KurobbsClientException("; ".join(map(str, self.exceptions)))


def main():
    # Configure logging as early as possible to avoid leaking secrets in GitHub Actions logs.
    # 获取环境变量中的TOKEN
    token_str = os.getenv("TOKEN", "").strip()
    if not token_str:
        logger.error("TOKEN environment variable is not set.")
        sys.exit(1)

    # 将字符串按逗号分割成列表，并去除首尾空格
    tokens = [token.strip() for token in token_str.split(",") if token.strip()]
    
    if not tokens:
        logger.error("No valid tokens found in TOKEN environment variable.")
        sys.exit(1)

    # 配置日志记录器
    configure_logger(
        debug=parse_bool(os.getenv("DEBUG", "")),
        secrets=[
            token_str, # 将原始字符串加入脱敏列表
            os.getenv("BARK_DEVICE_KEY", ""),
            os.getenv("BARK_SERVER_URL", ""),
            os.getenv("SERVER3_SEND_KEY", ""),
        ],
    )

    try:
        settings = Settings.load()
    except SettingsError as exc:
        logger.error(str(exc))
        sys.exit(1)

    notifier = NotificationService(settings)
    
    # 总结所有账号的结果
    all_results = []
    all_exceptions = []

    # 遍历每个Token进行处理
    for i, token in enumerate(tokens, 1):
        logger.info(f"处理第 {i} 个账号")
        try:
            kurobbs = KurobbsClient(token)
            kurobbs.start()
            if kurobbs.msg:
                all_results.append(f"账号{i}: {kurobbs.msg}")
        except KurobbsClientException as e:
            error_msg = f"账号{i} 错误: {str(e)}"
            logger.error(error_msg)
            all_exceptions.append(error_msg)
        except Exception as e:  # noqa: BLE001
            error_msg = f"账号{i} 发生未预期错误: {str(e)}"
            logger.exception(error_msg)
            all_exceptions.append(error_msg)

      if i < len(tokens):
            logger.info("休眠60秒...")
            time.sleep(60)

    # 发送最终通知
    final_message = ""
    if all_results:
        final_message += "✅ 签到成功汇总:\n" + "\n".join(all_results) + "\n\n"
    if all_exceptions:
        final_message += "❌ 失败汇总:\n" + "\n".join(all_exceptions)

    if final_message:
        notifier.send(final_message)
    elif not all_results: # 如果没有成功也没有失败（理论上不太可能）
        notifier.send("所有账号签到任务已完成，但未获取到具体结果。")


if __name__ == "__main__":
    main()
