from typing import Optional

import asyncpg

from bot.config import db_config


async def get_postgres_saver():
    from langgraph.checkpoint.postgres import PostgresSaver
    conn_string = db_config.connection_string
    saver = PostgresSaver.from_conn_string(conn_string)
    await saver.setup()
    return saver


async def registrar_ou_atualizar_contato(
    lid: str,
    numero_real: Optional[str],
    nome_usuario: str,
    real_jid: Optional[str] = None,
) -> Optional[str]:
    conn = None
    try:
        conn = await asyncpg.connect(db_config.connection_string)
        row = await conn.fetchrow(
            """
            INSERT INTO mapa_contatos (lid, numero_real, nome_usuario, real_jid)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (lid) DO UPDATE SET
                numero_real = EXCLUDED.numero_real,
                nome_usuario = EXCLUDED.nome_usuario,
                real_jid = COALESCE(EXCLUDED.real_jid, mapa_contatos.real_jid)
            RETURNING numero_real
            """,
            lid,
            numero_real,
            nome_usuario,
            real_jid,
        )
        return row["numero_real"] if row else None
    except Exception:
        return None
    finally:
        if conn:
            await conn.close()


async def consultar_memoria_contato(lid: str) -> Optional[dict]:
    conn = None
    try:
        conn = await asyncpg.connect(db_config.connection_string)
        row = await conn.fetchrow(
            "SELECT numero_real, nome_usuario, real_jid FROM mapa_contatos WHERE lid = $1 LIMIT 1",
            lid,
        )
        if row:
            return {
                "numero_real": row["numero_real"],
                "nome_usuario": row["nome_usuario"],
                "real_jid": row["real_jid"],
            }
        return None
    except Exception:
        return None
    finally:
        if conn:
            await conn.close()


async def consultar_contato_por_lid(lid: str) -> Optional[dict]:
    return await consultar_memoria_contato(lid)


async def migrar_banco():
    conn = None
    try:
        conn = await asyncpg.connect(db_config.connection_string)
        await conn.execute(
            "ALTER TABLE mapa_contatos ADD COLUMN IF NOT EXISTS real_jid VARCHAR(255)"
        )
        logger = __import__("logging").getLogger(__name__)
        logger.info("Coluna real_jid verificada/criada na tabela mapa_contatos")
    except Exception as e:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("Nao foi possivel migrar o banco: %s", e)
    finally:
        if conn:
            await conn.close()
