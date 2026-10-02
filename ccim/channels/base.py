"""IM 渠道的通用接口。飞书、以后的微信、企业微信各实现一个子类。

卡片用和渠道无关的写法描述，由各渠道自己渲染：
    {"title": "工作中 · 0:23", "color": "blue|green|orange|red|grey", "body": "markdown 正文",
     "buttons": [{"text": "允许", "style": "primary|danger|default", "value": {...}}], "note": "灰色小字"}
没有 title 时不要标题栏；只有 note 时就是一行小字。
"""
from dataclasses import dataclass, field


def address(chat_id, thread_id=None):
    """对话地址：普通聊天就是 chat_id；话题是「chat_id|thread_id」，往这个地址发的消息都进话题。"""
    return f"{chat_id}|{thread_id}" if thread_id else chat_id


def parse_address(addr):
    chat_id, _, thread_id = addr.partition("|")
    return chat_id, thread_id or None


@dataclass
class Incoming:
    chat_id: str
    chat_type: str                 # p2p 私聊 / group 群聊
    sender_id: str
    message_id: str
    text: str                      # 已去掉 @机器人 的占位符
    mentioned_bot: bool = False
    files: list = field(default_factory=list)   # [(类型 image/file, key, 文件名)]，由 download 下载
    kind: str = "text"                          # 原始消息类型
    thread_id: str = None                       # 在话题里发的消息才有
    root_id: str = None                         # 话题的第一条消息


@dataclass
class CardAction:
    chat_id: str
    message_id: str
    operator_id: str
    value: dict


class Channel:
    name = ""

    @staticmethod
    def pair(project_path):
        """交互式配对（扫码），返回要写进登记表的条目，其中 secret 字段由调用方存进钥匙串后删掉。"""
        raise NotImplementedError

    def __init__(self, project_path, entry, secret, handlers):
        """handlers: on_message(Incoming)、on_action(CardAction)、on_bot_added(chat_id)，都是协程函数。
        on_action 返回 (新卡片或 None, 提示文字或 None)，渠道用它在回调里直接更新卡片。"""
        self.project_path, self.entry, self.secret, self.handlers = project_path, entry, secret, handlers

    async def start(self):
        raise NotImplementedError

    async def stop(self):
        pass

    # 下面的 chat_id 都可以是 address() 生成的话题地址

    async def send_text(self, chat_id, markdown, reply_to=None, uid=None):
        """uid：同一条消息重发时传同一个，渠道据此去重。"""
        raise NotImplementedError

    async def send_card(self, chat_id, card, reply_to=None):
        """返回消息 id，之后可以 update_card。"""
        raise NotImplementedError

    async def update_card(self, message_id, card):
        raise NotImplementedError

    async def send_to_user(self, user_id, markdown):
        """主动私聊某人，返回和他的私聊 chat_id。"""
        raise NotImplementedError

    async def chat_name(self, chat_id):
        return ""

    async def get_text(self, message_id):
        """读一条消息的文字内容（用于告诉 Claude 话题是针对哪条消息开的）。"""
        return ""

    async def delete_message(self, message_id):
        raise NotImplementedError

    async def send_file(self, chat_id, path):
        raise NotImplementedError

    async def download(self, message_id, kind, key, name, folder):
        """把收到的图片、文件下载到 folder，返回本地路径。"""
        raise NotImplementedError

    async def add_reaction(self, message_id, emoji=None):
        """给用户的消息加个表情表示收到了，返回之后删除要用的参数；不支持的渠道返回 None。"""
        return None

    async def remove_reaction(self, message_id, reaction_id):
        pass

    def connected(self):
        return True

