"""Structured API error payload helpers."""

ERROR_ACTION = "如果方便的话，可以复制以下的错误码，并描述错误产生的情况，提交给 fedrick1plela755@gmail.com 来帮助改进："

ERRORS = {
    "MISSING_BODY": "Missing body",
    "MISSING_INPUT": "Missing URL or content",
    "MISSING_CHUNK": "Missing chunk",
    "MISSING_TRANSLATED_TEXT": "Missing translated_text",
    "MISSING_API_KEY": "Server is missing DASHSCOPE_API_KEY",
    "UNKNOWN_ACTION": "Unknown action",
    "DATABASE_NOT_CONFIGURED": "Database is not configured",
    "VALIDATION_ERROR": "Invalid database payload",
    "RESTRICTED_POST_CONTENT": "这是付费/受限内容，请利用浏览器的阅读模式复制后手动输入，或通过浏览器保存 HTML（WebArchive 功能上线中）再重试翻译。",
    "PROVIDER_BAD_REQUEST": "The selected model could not process this request",
    "PROVIDER_UNAVAILABLE": "Translation service is temporarily unavailable",
    "INTERNAL_ERROR": "Internal server error",
}


def error_response(code, status=400, message=None):
    return status, {
        "ok": False,
        "error": {
            "code": code,
            "message": message or ERRORS.get(code, ERRORS["INTERNAL_ERROR"]),
            "action": ERROR_ACTION,
        },
    }
