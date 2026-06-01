import asyncio
import os
import sys
from typing import Callable

from dotenv import load_dotenv
from loguru import logger


load_dotenv()


SUPPORTED_BRIDGES = {"dingtalk", "feishu", "wecom", "qqbot"}
COMMON_REQUIRED_ENV = {
    "ACCESS_KEY_ID": "阿里云 AccessKey ID",
    "ACCESS_SECRET": "阿里云 AccessKey Secret",
}
BRIDGE_REQUIRED_ENV = {
    "dingtalk": {
        "DINGTALK_APP_CLIENT_ID": "钉钉应用 Client ID",
        "DINGTALK_APP_CLIENT_SECRET": "钉钉应用 Client Secret",
    },
    "feishu": {
        "FEISHU_APP_ID": "飞书应用 App ID",
        "FEISHU_APP_SECRET": "飞书应用 App Secret",
    },
    "wecom": {
        "WECOM_BOT_ID": "企业微信 AI Bot ID",
        "WECOM_SECRET": "企业微信 AI Bot Secret",
    },
    "qqbot": {
        "QQ_APP_ID": "QQ Bot AppID",
        "QQ_CLIENT_SECRET": "QQ Bot Client Secret",
    },
}
BRIDGE_SECURITY_PREFIX = {
    "dingtalk": "DINGTALK",
    "feishu": "FEISHU",
    "wecom": "WECOM",
    "qqbot": "QQ",
}
ALLOW_POLICY_VALUES = {"disabled", "allowlist", "open"}
BRIDGE_CREDENTIAL_HINTS = {
    "dingtalk": "DINGTALK_APP_CLIENT_ID / DINGTALK_APP_CLIENT_SECRET",
    "feishu": "FEISHU_APP_ID / FEISHU_APP_SECRET",
    "wecom": "WECOM_BOT_ID / WECOM_SECRET",
    "qqbot": "QQ_APP_ID / QQ_CLIENT_SECRET",
}
STARTUP_CHECK_PRIORITY = {
    "qqbot": 0,
    "feishu": 1,
    "dingtalk": 2,
    "wecom": 3,
}
LOG_FILE_ENV = "RDS_COPILOT_LOG_FILE"
DEFAULT_LOG_FILE = "rds-copilot.log"
BRIDGE_RESTART_BASE_SECONDS_ENV = "RDS_BRIDGE_RESTART_BASE_SECONDS"
BRIDGE_RESTART_MAX_SECONDS_ENV = "RDS_BRIDGE_RESTART_MAX_SECONDS"
DEFAULT_BRIDGE_RESTART_BASE_SECONDS = 3.0
DEFAULT_BRIDGE_RESTART_MAX_SECONDS = 60.0
_LOGGING_CONFIGURED = False


class EnvironmentConfigurationError(RuntimeError):
    pass


def get_log_file_path() -> str:
    return os.getenv(LOG_FILE_ENV, os.path.join(os.getcwd(), DEFAULT_LOG_FILE))


def configure_file_logging():
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        get_log_file_path(),
        rotation="10 MB",
        retention=5,
        enqueue=True,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )
    _LOGGING_CONFIGURED = True


def parse_bridge_names(raw_value: str = None) -> list[str]:
    raw_value = os.getenv("RDS_BOT_BRIDGES", "dingtalk") if raw_value is None else raw_value
    normalized_value = (raw_value or "dingtalk").strip().lower()
    if normalized_value == "all":
        normalized_value = "dingtalk,feishu,wecom,qqbot"

    bridge_names = []
    for item in normalized_value.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in SUPPORTED_BRIDGES:
            supported = ", ".join(sorted(SUPPORTED_BRIDGES))
            raise ValueError(f"Unsupported bridge: {name}. Supported bridges: {supported}")
        if name not in bridge_names:
            bridge_names.append(name)
    return bridge_names or ["dingtalk"]


def _env_present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def validate_startup_environment(bridge_names: list[str]) -> None:
    missing_items: list[tuple[str, str]] = []
    for name, description in COMMON_REQUIRED_ENV.items():
        if not _env_present(name):
            missing_items.append((name, description))

    for bridge_name in bridge_names:
        for name, description in BRIDGE_REQUIRED_ENV[bridge_name].items():
            if not _env_present(name):
                missing_items.append((name, f"{bridge_name}: {description}"))
        prefix = BRIDGE_SECURITY_PREFIX[bridge_name]
        for scope in ("DM", "GROUP"):
            env_name = f"{prefix}_{scope}_ALLOW_POLICY"
            policy = os.getenv(env_name, "").strip().lower()
            if policy and policy not in ALLOW_POLICY_VALUES:
                allowed = ", ".join(sorted(ALLOW_POLICY_VALUES))
                missing_items.append((env_name, f"{bridge_name}: 取值只能是 {allowed}"))

    if not missing_items:
        return

    lines = [
        "RDS Copilot Bot Gateway 配置错误：请修正以下环境变量。",
        f"已选择 bridge：{', '.join(bridge_names)}",
        "",
        "问题：",
    ]
    lines.extend(f"- {name}: {description}" for name, description in missing_items)
    lines.extend(
        [
            "",
            "处理方法：在当前运行目录的 .env 文件中补齐这些变量，或在启动前 export。",
            "注意：请不要把 Secret 打印到日志或提交到代码仓库。",
        ]
    )
    raise EnvironmentConfigurationError("\n".join(lines))


def validate_bridge_startup(bridge_name: str) -> None:
    if bridge_name == "dingtalk":
        from bridges.dingtalk import validate_dingtalk_startup

        validate_dingtalk_startup()
        return
    if bridge_name == "feishu":
        from bridges.feishu import validate_feishu_startup

        validate_feishu_startup()
        return
    if bridge_name == "wecom":
        from bridges.wecom import validate_wecom_startup

        validate_wecom_startup()
        return
    if bridge_name == "qqbot":
        from bridges.qq import validate_qq_startup

        validate_qq_startup()
        return
    raise ValueError(f"Unsupported bridge: {bridge_name}")


def validate_selected_bridge_startup(bridge_names: list[str]) -> None:
    for bridge_name in sorted(bridge_names, key=lambda name: STARTUP_CHECK_PRIORITY.get(name, 99)):
        try:
            validate_bridge_startup(bridge_name)
        except Exception as e:
            lines = [
                "RDS Copilot Bot Gateway 启动检查失败：至少一个 IM 平台无法完成启动鉴权。",
                "为避免进程看似启动但机器人不可用，本次启动已停止。",
                "",
                "失败平台：",
                f"- {bridge_name}: {e.__class__.__name__}: {e}",
            ]
            raise EnvironmentConfigurationError("\n".join(lines)) from e


def format_bridge_failure_message(bridge_name: str, error: BaseException) -> str:
    credential_hint = BRIDGE_CREDENTIAL_HINTS.get(bridge_name, "该 bridge 的凭证环境变量")
    return (
        f"{bridge_name} bridge 启动/运行失败。请检查 {credential_hint} 是否正确，"
        f"并确认机器人权限已开通。原始错误：{error.__class__.__name__}: {error}"
    )


def _read_positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def calculate_restart_delay(restart_index: int) -> float:
    base = _read_positive_float_env(BRIDGE_RESTART_BASE_SECONDS_ENV, DEFAULT_BRIDGE_RESTART_BASE_SECONDS)
    max_delay = _read_positive_float_env(BRIDGE_RESTART_MAX_SECONDS_ENV, DEFAULT_BRIDGE_RESTART_MAX_SECONDS)
    return min(max_delay, base * (2 ** max(0, restart_index)))


async def supervise_bridge(
    bridge_name: str,
    runner: Callable[[], None],
    *,
    sleep: Callable[[float], object] = asyncio.sleep,
    max_restarts: int | None = None,
):
    restart_count = 0
    while True:
        try:
            await asyncio.to_thread(runner)
            logger.warning("{} bridge 已退出，将自动重启", bridge_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            message = format_bridge_failure_message(bridge_name, e)
            print(message, file=sys.stderr)
            logger.error("{}；将自动重启", message)

        if max_restarts is not None and restart_count >= max_restarts:
            return
        delay = calculate_restart_delay(restart_count)
        restart_count += 1
        await sleep(delay)


async def run_selected_bridges(
    bridge_names: list[str],
    *,
    max_restarts: int | None = None,
):
    tasks = []
    if "dingtalk" in bridge_names:
        from bridges.dingtalk import run_dingtalk_bridge

        tasks.append(asyncio.create_task(supervise_bridge("dingtalk", run_dingtalk_bridge, max_restarts=max_restarts)))
    if "feishu" in bridge_names:
        from bridges.feishu import run_feishu_bridge

        tasks.append(asyncio.create_task(supervise_bridge("feishu", run_feishu_bridge, max_restarts=max_restarts)))
    if "wecom" in bridge_names:
        from bridges.wecom import run_wecom_bridge

        tasks.append(asyncio.create_task(supervise_bridge("wecom", run_wecom_bridge, max_restarts=max_restarts)))
    if "qqbot" in bridge_names:
        from bridges.qq import run_qq_bridge

        tasks.append(asyncio.create_task(supervise_bridge("qqbot", run_qq_bridge, max_restarts=max_restarts)))
    await asyncio.gather(*tasks)


def main():
    configure_file_logging()
    try:
        bridge_names = parse_bridge_names()
        validate_startup_environment(bridge_names)
        validate_selected_bridge_startup(bridge_names)
        logger.info(f"Starting bot bridges: {bridge_names}")
        asyncio.run(run_selected_bridges(bridge_names))
    except (EnvironmentConfigurationError, ValueError) as e:
        print(str(e), file=sys.stderr)
        logger.error(str(e))
        raise SystemExit(2) from e


if __name__ == "__main__":  # pragma: no cover - script entrypoint
    main()
