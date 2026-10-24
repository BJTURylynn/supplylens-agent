"""Recognize provider notices returned as successful HTTP chat messages."""


class ProviderQuotaError(Exception):
    pass


def validate_provider_reply(message) -> None:
    content = getattr(message, "content", "")
    if isinstance(content, str) and content.strip().startswith(
        "Sorry, to prevent abuse of free resources, accounts that have not been recharged can only try"
    ):
        raise ProviderQuotaError("模型平台提示未充值账号的试用次数已用完，请在平台控制台核对免费配额。")
