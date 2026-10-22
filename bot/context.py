"""Contexto da requisição em andamento (usuário que falou com o bot).

Usado pelas tools para saber o JID de quem perguntou, sem precisar repassar
estado manualmente. O api.py define o valor antes de invocar o agente; como
cada webhook roda em sua própria task do asyncio, o contextvar não vaza
entre usuários.
"""
import contextvars

_current_user_jid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_jid", default=""
)


def set_current_user(jid: str) -> None:
    _current_user_jid.set(jid or "")


def get_current_user() -> str:
    return _current_user_jid.get()
