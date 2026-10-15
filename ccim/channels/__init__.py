NAMES = {"feishu": "飞书", "weixin": "微信", "web": "网页"}


def load(name):
    """按需导入渠道：飞书 SDK 导入要好几秒，用微信的项目不必加载它。"""
    if name == "web":
        from ..platform import WebChannel
        return WebChannel
    if name == "weixin":
        from .weixin import Weixin
        return Weixin
    from .feishu import Feishu
    return Feishu


async def send_once(path, entry, text, chat_id=None):
    """不在 ccim 进程里时发一条消息：发到 chat_id，不写就私聊主人。"""
    from .. import registry
    ch = load(entry["channel"])(path, entry, registry.get_secret(entry["app_id"]), {})
    if entry["channel"] == "weixin":          # 微信的连接在 start 里建，这里只发消息不收，单独建一个
        import httpx
        ch.http = httpx.AsyncClient()
    try:
        if chat_id:
            await ch.send_text(chat_id, text)
        else:                                 # 私聊主人：第一段开启私聊，剩下的按普通消息发（会自动拆条）
            from .base import split_text
            parts = split_text(text, 3000)
            cid = await ch.send_to_user(entry["owner_open_id"], parts[0])
            if cid and len(parts) > 1:
                await ch.send_text(cid, "\n".join(parts[1:]))
    finally:
        await ch.stop()
