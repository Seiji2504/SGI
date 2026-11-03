from functools import lru_cache
from typing import Annotated, Iterator

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings, get_settings


@lru_cache
def get_pool() -> ConnectionPool:
    settings = get_settings()
    conninfo = settings.database_url or make_conninfo(
        host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
        user=settings.db_user, password=settings.db_password, sslmode=settings.db_sslmode,
    )
    return ConnectionPool(
        conninfo=conninfo,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )


@lru_cache
def get_jwks_client() -> PyJWKClient:
    settings = get_settings()
    if not settings.cognito_user_pool_id:
        raise RuntimeError("COGNITO_USER_POOL_ID no está configurado")
    return PyJWKClient(
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
        f"{settings.cognito_user_pool_id}/.well-known/jwks.json",
        cache_keys=True,
        lifespan=3600,
        timeout=5,
    )


def get_db() -> Iterator[Connection]:
    with get_pool().connection() as connection:
        with connection.transaction():
            yield connection


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Connection = Depends(get_db),
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Inicia sesión para continuar")
    settings = get_settings()
    token = authorization.split(" ", 1)[1].strip()
    try:
        signing_key = get_jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=(f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
                    f"{settings.cognito_user_pool_id}"),
            options={"verify_aud": False},
        )
        if claims.get("token_use") != "access" or claims.get("client_id") != settings.cognito_client_id:
            raise jwt.InvalidTokenError("Token de acceso no válido")
        user_id = claims["sub"]
        email = claims.get("email") or claims.get("username", "")
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = [groups]
        is_platform_admin = "platform-admin" in groups
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Sesión inválida o vencida") from exc

    with db.transaction():
        db.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        db.execute("SELECT set_config('app.platform_admin', %s, true)", ("true" if is_platform_admin else "false",))
        db.execute(
            "INSERT INTO app_user (id, email) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (user_id, email),
        )
        account_row = db.execute("SELECT is_active FROM app_user WHERE id=%s", (user_id,)).fetchone()
        if not account_row or not account_row["is_active"]:
            raise HTTPException(status_code=403, detail="Esta cuenta fue desactivada")
        membership = db.execute(
            "SELECT warehouse_id, role FROM warehouse_member WHERE user_id = %s "
            "ORDER BY created_at LIMIT 1",
            (user_id,),
        ).fetchone()
        db.execute("SELECT set_config('app.warehouse_id', %s, true)", (str(membership["warehouse_id"]) if membership else "",))
    return {
        "id": user_id,
        "email": email,
        "warehouse_id": str(membership["warehouse_id"]) if membership else None,
        "role": membership["role"] if membership else None,
        "is_platform_admin": is_platform_admin,
    }


CurrentUser = Annotated[dict, Depends(get_current_user)]
Database = Annotated[Connection, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
