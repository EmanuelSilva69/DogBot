from typing import Optional

from langgraph.graph import MessagesState


class BotState(MessagesState):
    lid: str = ""
    remote_jid: str = ""
    message_id: str = ""
    push_name: str = ""
    is_group: bool = False
    real_number: Optional[str] = None
    target_private_jid: Optional[str] = None
    msg_original: str = ""
    termo: str = ""
    intent: str = ""
    search_result: Optional[dict] = None
    coupon_result: Optional[dict] = None
    formatted_message: str = ""
