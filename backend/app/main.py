import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from psycopg import Connection, errors
from uuid import UUID

from .config import get_settings
from .dependencies import CurrentUser, Database
from .schemas import AccountEnrollment, Movement, MovementInput, PlatformMembershipRoleUpdate, Product, ProductInput, ProductPage, WarehouseCreate, WarehouseMemberInput, WarehouseMemberRoleUpdate

settings = get_settings()
app = FastAPI(title="Menoss Inventario API", version="2.0.0", description="API de inventario multi-bodega de Menoss")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Temporal para pruebas
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Warehouse-ID"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "menoss-inventory-api"}


def require_warehouse(user: dict, warehouse_id: UUID | None, db: Connection, write: bool = False) -> str:
    selected = str(warehouse_id or user["warehouse_id"])
    row = db.execute(
        "SELECT role FROM warehouse_member WHERE warehouse_id = %s AND user_id = %s",
        (selected, user["id"]),
    ).fetchone()
    if not row and not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="No tienes acceso a esta bodega")
    if write and row and row["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Tu rol solo permite consultar esta bodega")
    db.execute("SELECT set_config('app.warehouse_id', %s, true)", (selected,))
    return selected


def db_error(exc: Exception) -> HTTPException:
    if isinstance(exc, errors.UniqueViolation):
        return HTTPException(status_code=409, detail="Ya existe un producto con ese código SKU")
    if isinstance(exc, errors.CheckViolation):
        return HTTPException(status_code=422, detail="Los datos no cumplen las reglas de inventario")
    if isinstance(exc, errors.ForeignKeyViolation):
        return HTTPException(status_code=404, detail="La cuenta destino no existe; debe iniciar sesión primero")
    return HTTPException(status_code=503, detail="El servicio de inventario no está disponible temporalmente")


def product_json(row: dict) -> dict:
    return {
        "id": str(row["id"]), "warehouse_id": str(row["warehouse_id"]),
        "name": row["name"], "category": row["category"], "sku": row["sku"],
        "unit": row["unit"], "price": row["price"], "quantity": row["quantity"],
        "min_stock": row["min_stock"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


PRODUCT_SELECT = """
SELECT p.id, p.warehouse_id, p.name, p.category, p.sku, p.unit, p.price,
       d.quantity, d.min_stock, p.created_at, GREATEST(p.updated_at, d.updated_at) AS updated_at
FROM product p JOIN product_detail d ON d.product_id = p.id
"""


@app.get("/api/products", response_model=ProductPage)
def list_products(
    user: CurrentUser,
    db: Database,
    search: str | None = Query(default=None, max_length=120),
    category: str | None = Query(default=None, max_length=80),
    low_stock: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    warehouse_id: UUID | None = None,
) -> dict:
    warehouse_id = require_warehouse(user, warehouse_id, db)
    clauses = ["p.warehouse_id = %s", "p.archived_at IS NULL"]
    params: list = [warehouse_id]
    if search and search.strip():
        clauses.append("(p.name ILIKE %s OR COALESCE(p.sku, '') ILIKE %s)")
        pattern = f"%{search.strip()}%"
        params.extend([pattern, pattern])
    if category:
        clauses.append("p.category = %s")
        params.append(category)
    if low_stock:
        clauses.append("d.quantity <= d.min_stock")
    where = " AND ".join(clauses)
    try:
        total = db.execute(
            f"SELECT count(*) AS total FROM product p JOIN product_detail d ON d.product_id=p.id WHERE {where}",
            params,
        ).fetchone()["total"]
        rows = db.execute(
            PRODUCT_SELECT + f" WHERE {where} ORDER BY p.name, p.id LIMIT %s OFFSET %s",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        return {"items": [product_json(row) for row in rows], "total": total,
                "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}
    except Exception as exc:
        raise db_error(exc) from exc


@app.get("/api/products/categories", response_model=list[str])
def product_categories(user: CurrentUser, db: Database, warehouse_id: UUID | None = None) -> list[str]:
    warehouse_id = require_warehouse(user, warehouse_id, db)
    rows = db.execute(
        "SELECT DISTINCT category FROM product WHERE warehouse_id=%s AND archived_at IS NULL ORDER BY category",
        (warehouse_id,),
    ).fetchall()
    return [row["category"] for row in rows]


@app.get("/api/inventory/summary")
def inventory_summary(user: CurrentUser, db: Database, warehouse_id: UUID | None = None) -> dict:
    warehouse_id = require_warehouse(user, warehouse_id, db)
    row = db.execute(
        """SELECT count(*) AS product_count, coalesce(sum(d.quantity),0) AS total_units,
                  coalesce(sum(d.quantity*p.price),0) AS total_value,
                  count(*) FILTER (WHERE d.quantity <= d.min_stock) AS low_stock_count
           FROM product p JOIN product_detail d ON d.product_id=p.id
           WHERE p.warehouse_id=%s AND p.archived_at IS NULL""",
        (warehouse_id,),
    ).fetchone()
    return row


def write_stock_movement(db: Connection, user_id: str, warehouse_id: str, product_id: str,
                         movement_type: str, quantity: int, note: str | None) -> dict:
    detail = db.execute(
        "SELECT d.quantity FROM product_detail d JOIN product p ON p.id=d.product_id "
        "WHERE d.product_id=%s AND p.warehouse_id=%s AND p.archived_at IS NULL FOR UPDATE OF d",
        (product_id, warehouse_id),
    ).fetchone()
    if not detail:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    previous = detail["quantity"]
    if movement_type == "entrada":
        new_stock = previous + quantity
    elif movement_type == "salida":
        new_stock = previous - quantity
        if new_stock < 0:
            raise HTTPException(status_code=422, detail="La salida supera el stock disponible")
    else:
        new_stock = quantity
    db.execute("UPDATE product_detail SET quantity=%s WHERE product_id=%s", (new_stock, product_id))
    return db.execute(
        """INSERT INTO inventory_movement
           (warehouse_id, product_id, user_id, type, quantity, previous_stock, new_stock, note)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (warehouse_id, product_id, user_id, movement_type, quantity, previous, new_stock, note),
    ).fetchone()


@app.post("/api/products", response_model=Product, status_code=201)
def create_product(payload: ProductInput, user: CurrentUser, db: Database, warehouse_id: UUID | None = None) -> dict:
    warehouse_id = require_warehouse(user, warehouse_id, db, write=True)
    data = payload.model_dump()
    try:
        with db.transaction():
            row = db.execute(
                """INSERT INTO product (warehouse_id,name,category,sku,unit,price,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (warehouse_id, data["name"], data["category"], data["sku"], data["unit"], data["price"], user["id"]),
            ).fetchone()
            db.execute("INSERT INTO product_detail (product_id,quantity,min_stock) VALUES (%s,0,%s)",
                       (row["id"], data["min_stock"]))
            if data["quantity"]:
                write_stock_movement(db, user["id"], warehouse_id, str(row["id"]), "entrada", data["quantity"], "Stock inicial")
            result = db.execute(PRODUCT_SELECT + " WHERE p.id=%s", (row["id"],)).fetchone()
        return product_json(result)
    except (errors.UniqueViolation, errors.CheckViolation) as exc:
        raise db_error(exc) from exc


@app.patch("/api/products/{product_id}", response_model=Product)
def update_product(product_id: UUID, payload: ProductInput, user: CurrentUser, db: Database,
                   warehouse_id: UUID | None = None) -> dict:
    warehouse_id = require_warehouse(user, warehouse_id, db, write=True)
    data = payload.model_dump()
    try:
        with db.transaction():
            row = db.execute(
                """UPDATE product SET name=%s,category=%s,sku=%s,unit=%s,price=%s
                   WHERE id=%s AND warehouse_id=%s AND archived_at IS NULL RETURNING id""",
                (data["name"], data["category"], data["sku"], data["unit"], data["price"], product_id, warehouse_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Producto no encontrado")
            detail = db.execute("SELECT quantity FROM product_detail WHERE product_id=%s FOR UPDATE", (product_id,)).fetchone()
            db.execute("UPDATE product_detail SET min_stock=%s WHERE product_id=%s", (data["min_stock"], product_id))
            if detail["quantity"] != data["quantity"]:
                write_stock_movement(db, user["id"], warehouse_id, product_id, "ajuste", data["quantity"], "Ajuste desde edición de producto")
            result = db.execute(PRODUCT_SELECT + " WHERE p.id=%s", (product_id,)).fetchone()
        return product_json(result)
    except (errors.UniqueViolation, errors.CheckViolation) as exc:
        raise db_error(exc) from exc


@app.delete("/api/products/{product_id}", status_code=204)
def delete_product(product_id: UUID, user: CurrentUser, db: Database, warehouse_id: UUID | None = None) -> Response:
    warehouse_id = require_warehouse(user, warehouse_id, db, write=True)
    row = db.execute(
        "UPDATE product SET archived_at=now() WHERE id=%s AND warehouse_id=%s AND archived_at IS NULL RETURNING id",
        (product_id, warehouse_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return Response(status_code=204)


def movement_json(row: dict) -> dict:
    return {"id": str(row["id"]), "user_id": str(row["user_id"]), "product_id": str(row["product_id"]),
            "type": row["type"], "quantity": row["quantity"], "previous_stock": row["previous_stock"],
            "new_stock": row["new_stock"], "note": row["note"], "created_at": row["created_at"]}


@app.get("/api/products/{product_id}/movements", response_model=list[Movement])
def list_movements(product_id: UUID, user: CurrentUser, db: Database, warehouse_id: UUID | None = None) -> list[dict]:
    warehouse_id = require_warehouse(user, warehouse_id, db)
    rows = db.execute(
        "SELECT m.* FROM inventory_movement m JOIN product p ON p.id=m.product_id "
        "WHERE m.product_id=%s AND m.warehouse_id=%s ORDER BY m.created_at DESC LIMIT 100",
        (product_id, warehouse_id),
    ).fetchall()
    return [movement_json(row) for row in rows]


@app.post("/api/products/{product_id}/movements", response_model=Movement, status_code=201)
def create_movement(product_id: UUID, payload: MovementInput, user: CurrentUser, db: Database,
                    warehouse_id: UUID | None = None) -> dict:
    warehouse_id = require_warehouse(user, warehouse_id, db, write=True)
    try:
        with db.transaction():
            row = write_stock_movement(db, user["id"], warehouse_id, product_id, payload.type,
                                       payload.quantity, payload.note)
        return movement_json(row)
    except errors.CheckViolation as exc:
        raise db_error(exc) from exc


@app.get("/api/account")
def account(user: CurrentUser, db: Database) -> dict:
    row = db.execute("SELECT full_name FROM app_user WHERE id=%s", (user["id"],)).fetchone()
    return {"id": user["id"], "email": user["email"], "full_name": row["full_name"] if row else "",
            "role": user["role"], "is_platform_admin": user.get("is_platform_admin", False)}


@app.patch("/api/account")
def update_account(payload: dict, user: CurrentUser, db: Database) -> dict:
    full_name = str(payload.get("full_name", "")).strip()
    if len(full_name) > 120:
        raise HTTPException(status_code=422, detail="El nombre no puede superar 120 caracteres")
    db.execute("UPDATE app_user SET full_name=%s WHERE id=%s", (full_name, user["id"]))
    return {"full_name": full_name}


@app.get("/api/warehouses")
def list_warehouses(user: CurrentUser, db: Database) -> list[dict]:
    if user.get("is_platform_admin"):
        return db.execute(
            """SELECT w.id, w.name, coalesce(wm.role, 'platform_admin') AS role
               FROM warehouse w LEFT JOIN warehouse_member wm
                 ON wm.warehouse_id=w.id AND wm.user_id=%s
               ORDER BY lower(w.name), w.id""",
            (user["id"],),
        ).fetchall()
    return db.execute(
        "SELECT w.id,w.name,wm.role FROM warehouse w JOIN warehouse_member wm ON wm.warehouse_id=w.id "
        "WHERE wm.user_id=%s ORDER BY w.created_at", (user["id"],),
    ).fetchall()


@app.post("/api/warehouses", status_code=201)
def create_warehouse(payload: WarehouseCreate, user: CurrentUser, db: Database) -> dict:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede crear bodegas")
    name = payload.name
    warehouse = db.execute("INSERT INTO warehouse(name,created_by) VALUES (%s,%s) RETURNING id,name",
                           (name, user["id"])).fetchone()
    return {**warehouse, "role": "platform_admin"}


@app.patch("/api/admin/warehouses/{warehouse_id}")
def update_global_warehouse(
    warehouse_id: UUID, payload: WarehouseCreate, user: CurrentUser, db: Database,
) -> dict:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede editar bodegas")
    row = db.execute(
        "UPDATE warehouse SET name=%s WHERE id=%s RETURNING id,name",
        (payload.name, warehouse_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Bodega no encontrada")
    return {**row, "role": "platform_admin"}


@app.delete("/api/admin/warehouses/{warehouse_id}", status_code=204)
def delete_global_warehouse(warehouse_id: UUID, user: CurrentUser, db: Database) -> Response:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede eliminar bodegas")
    with db.transaction():
        row = db.execute(
            "SELECT id FROM warehouse WHERE id=%s FOR UPDATE", (warehouse_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bodega no encontrada")
        selected = str(warehouse_id)
        db.execute("SELECT set_config('app.warehouse_id', %s, true)", (selected,))
        db.execute("DELETE FROM inventory_movement WHERE warehouse_id=%s", (selected,))
        db.execute(
            "DELETE FROM product_detail WHERE product_id IN (SELECT id FROM product WHERE warehouse_id=%s)",
            (selected,),
        )
        db.execute("DELETE FROM product WHERE warehouse_id=%s", (selected,))
        db.execute("DELETE FROM warehouse_member WHERE warehouse_id=%s", (selected,))
        db.execute("DELETE FROM warehouse WHERE id=%s", (selected,))
    return Response(status_code=204)


@app.get("/api/registration/warehouses")
def registration_warehouses(db: Database) -> list[dict]:
    return db.execute("SELECT id,name FROM list_registration_warehouses()").fetchall()


@app.post("/api/account/registration", status_code=201)
def enroll_account(payload: AccountEnrollment, user: CurrentUser, db: Database) -> dict:
    try:
        row = db.execute(
            "SELECT register_current_user(%s,%s) AS role",
            (payload.warehouse_id, payload.full_name),
        ).fetchone()
    except errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="Esta cuenta ya pertenece a una bodega") from exc
    except errors.NoDataFound as exc:
        raise HTTPException(status_code=404, detail="La bodega seleccionada no existe") from exc
    except errors.InsufficientPrivilege as exc:
        raise HTTPException(status_code=403, detail="No se pudo completar el alta de esta cuenta") from exc
    return {"user_id": user["id"], "warehouse_id": str(payload.warehouse_id), "role": row["role"]}


@app.post("/api/warehouses/{warehouse_id}/members", status_code=201)
def add_warehouse_member(warehouse_id: UUID, payload: WarehouseMemberInput, user: CurrentUser, db: Database) -> dict:
    actor = db.execute("SELECT role FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s",
                       (warehouse_id, user["id"])).fetchone()
    if not user.get("is_platform_admin") and (not actor or actor["role"] not in {"owner", "admin"}):
        raise HTTPException(status_code=403, detail="Solo propietarios y administradores pueden agregar miembros")
    member_id = str(payload.user_id)
    role = payload.role
    existing = db.execute("SELECT role FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s",
                          (warehouse_id, member_id)).fetchone()
    if existing and existing["role"] == "owner" and not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="No se puede cambiar el rol del propietario")
    if actor and actor["role"] == "admin" and (role == "admin" or (existing and existing["role"] == "admin")):
        raise HTTPException(status_code=403, detail="Solo el propietario puede asignar o cambiar administradores")
    try:
        db.execute("INSERT INTO warehouse_member(warehouse_id,user_id,role) VALUES (%s,%s,%s) "
                   "ON CONFLICT (warehouse_id,user_id) DO UPDATE SET role=EXCLUDED.role",
                   (warehouse_id, member_id, role))
    except errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="La persona debe iniciar sesión al menos una vez antes de agregarla") from exc
    return {"warehouse_id": warehouse_id, "user_id": member_id, "role": role}


@app.get("/api/warehouses/{warehouse_id}/members")
def list_warehouse_members(warehouse_id: UUID, user: CurrentUser, db: Database) -> dict:
    selected = require_warehouse(user, warehouse_id, db)
    actor = db.execute(
        "SELECT role FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s",
        (selected, user["id"]),
    ).fetchone()
    if not user.get("is_platform_admin") and (not actor or actor["role"] not in {"owner", "admin"}):
        raise HTTPException(status_code=403, detail="Solo propietarios y administradores pueden ver la lista de usuarios")
    rows = db.execute(
        """SELECT wm.user_id, wm.role, wm.created_at, au.email, au.full_name
           FROM warehouse_member wm JOIN app_user au ON au.id=wm.user_id
           WHERE wm.warehouse_id=%s
           ORDER BY CASE wm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'member' THEN 2 ELSE 3 END,
                    lower(au.email), wm.user_id""",
        (selected,),
    ).fetchall()
    return {"warehouse_id": selected, "current_role": actor["role"] if actor else "platform_admin", "members": rows}


@app.patch("/api/warehouses/{warehouse_id}/members/{member_id}")
def update_warehouse_member_role(
    warehouse_id: UUID, member_id: UUID, payload: WarehouseMemberRoleUpdate, user: CurrentUser, db: Database,
) -> dict:
    selected = require_warehouse(user, warehouse_id, db, write=True)
    with db.transaction():
        actor = db.execute(
            "SELECT role FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s FOR UPDATE",
            (selected, user["id"]),
        ).fetchone()
        if actor["role"] not in {"owner", "admin"}:
            raise HTTPException(status_code=403, detail="Solo propietarios y administradores pueden cambiar permisos")
        target = db.execute(
            "SELECT role FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s FOR UPDATE",
            (selected, member_id),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Usuario no encontrado en esta bodega")
        if target["role"] == "owner":
            raise HTTPException(status_code=403, detail="El rol de propietario no se puede cambiar desde esta pantalla")
        if actor["role"] == "admin" and (target["role"] == "admin" or payload.role == "admin"):
            raise HTTPException(status_code=403, detail="Solo el propietario puede asignar o cambiar administradores")
        db.execute(
            "UPDATE warehouse_member SET role=%s WHERE warehouse_id=%s AND user_id=%s",
            (payload.role, selected, member_id),
        )
    return {"warehouse_id": selected, "user_id": str(member_id), "role": payload.role}


@app.get("/api/admin/users")
def list_all_users(user: CurrentUser, db: Database) -> dict:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede ver todas las cuentas")
    rows = db.execute(
        """SELECT au.id, au.email, au.full_name, au.is_active,
                  wm.warehouse_id, w.name AS warehouse_name, wm.role, wm.created_at AS joined_at
           FROM app_user au
           LEFT JOIN warehouse_member wm ON wm.user_id=au.id
           LEFT JOIN warehouse w ON w.id=wm.warehouse_id
           ORDER BY lower(au.full_name), lower(au.email), lower(w.name), wm.warehouse_id"""
    ).fetchall()
    users: dict[str, dict] = {}
    cognito_user_ids: set[str] = set()
    for row in rows:
        user_id = str(row["id"])
        item = users.setdefault(user_id, {
            "id": user_id, "email": row["email"], "full_name": row["full_name"],
            "is_active": row["is_active"], "memberships": [],
        })
        if row["warehouse_id"]:
            item["memberships"].append({
                "warehouse_id": str(row["warehouse_id"]), "warehouse_name": row["warehouse_name"],
                "role": row["role"], "joined_at": row["joined_at"],
            })
    settings = get_settings()
    cognito = boto3.client("cognito-idp", region_name=settings.cognito_region)
    paginator = cognito.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=settings.cognito_user_pool_id):
        for cognito_user in page.get("Users", []):
            attrs = {item["Name"]: item["Value"] for item in cognito_user.get("Attributes", [])}
            user_id = attrs.get("sub")
            if not user_id:
                continue
            cognito_user_ids.add(user_id)
            db.execute(
                """INSERT INTO app_user(id,email,full_name,is_active) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (user_id, attrs.get("email", ""), attrs.get("name", ""), cognito_user.get("Enabled", True)),
            )
            item = users.setdefault(user_id, {
                "id": user_id, "email": attrs.get("email", ""), "full_name": attrs.get("name", ""),
                "is_active": cognito_user.get("Enabled", True), "cognito_exists": True, "memberships": [],
            })
            item["cognito_exists"] = True
            if item.get("is_active") and not cognito_user.get("Enabled", True):
                item["is_active"] = False
            if not item.get("email"):
                item["email"] = attrs.get("email", "")
            if not item.get("full_name"):
                item["full_name"] = attrs.get("name", "")
    visible_users = [
        item for item in users.values()
        if item["id"] in cognito_user_ids or item["memberships"]
    ]
    for item in visible_users:
        item["cognito_exists"] = item["id"] in cognito_user_ids
        if not item["cognito_exists"]:
            item["is_active"] = False
    return {"users": sorted(visible_users, key=lambda item: ((item.get("full_name") or "").lower(), (item.get("email") or "").lower(), item["id"]))}


@app.patch("/api/admin/users/{member_id}/warehouses/{warehouse_id}")
def update_global_member_role(
    member_id: UUID, warehouse_id: UUID, payload: PlatformMembershipRoleUpdate,
    user: CurrentUser, db: Database,
) -> dict:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede cambiar permisos globalmente")
    with db.transaction():
        target = db.execute(
            "SELECT role FROM warehouse_member WHERE user_id=%s AND warehouse_id=%s FOR UPDATE",
            (member_id, warehouse_id),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="El usuario ya no pertenece a esa bodega")
        if payload.role == "owner" and target["role"] != "owner":
            db.execute(
                "UPDATE warehouse_member SET role='admin' WHERE warehouse_id=%s AND role='owner'",
                (warehouse_id,),
            )
        db.execute(
            "UPDATE warehouse_member SET role=%s WHERE user_id=%s AND warehouse_id=%s",
            (payload.role, member_id, warehouse_id),
        )
    return {"user_id": str(member_id), "warehouse_id": str(warehouse_id), "role": payload.role}


@app.delete("/api/admin/users/{member_id}", status_code=204)
def delete_global_user(member_id: UUID, user: CurrentUser, db: Database) -> Response:
    if not user.get("is_platform_admin"):
        raise HTTPException(status_code=403, detail="Solo el administrador global puede eliminar cuentas")
    if str(member_id) == user["id"]:
        raise HTTPException(status_code=409, detail="No puedes eliminar tu propia cuenta de administrador global")
    target = db.execute("SELECT email, is_active FROM app_user WHERE id=%s FOR UPDATE", (member_id,)).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada")
    if not target["email"]:
        raise HTTPException(status_code=409, detail="La cuenta no tiene un correo Cognito asociado")

    settings = get_settings()
    cognito = boto3.client("cognito-idp", region_name=settings.cognito_region)
    try:
        groups = cognito.admin_list_groups_for_user(
            UserPoolId=settings.cognito_user_pool_id, Username=target["email"],
        ).get("Groups", [])
        if any(group.get("GroupName") == "platform-admin" for group in groups):
            raise HTTPException(status_code=409, detail="No se puede eliminar una cuenta de administrador global")
        cognito.admin_delete_user(UserPoolId=settings.cognito_user_pool_id, Username=target["email"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "UserNotFoundException":
            if code == "AccessDeniedException":
                raise HTTPException(status_code=502, detail="AWS negó la eliminación en Cognito. Revisa que el rol ECS tenga cognito-idp:AdminDeleteUser; no se borraron sus bodegas.") from exc
            raise HTTPException(status_code=502, detail="Cognito no pudo eliminar la cuenta; no se borraron sus bodegas.") from exc

    with db.transaction():
        owned = db.execute(
            "SELECT warehouse_id FROM warehouse_member WHERE user_id=%s AND role='owner'",
            (member_id,),
        ).fetchall()
        for membership in owned:
            warehouse_id = str(membership["warehouse_id"])
            db.execute("SELECT set_config('app.warehouse_id', %s, true)", (warehouse_id,))
            db.execute("DELETE FROM inventory_movement WHERE warehouse_id=%s", (warehouse_id,))
            db.execute(
                "DELETE FROM product_detail WHERE product_id IN (SELECT id FROM product WHERE warehouse_id=%s)",
                (warehouse_id,),
            )
            db.execute("DELETE FROM product WHERE warehouse_id=%s", (warehouse_id,))
            db.execute("DELETE FROM warehouse WHERE id=%s", (warehouse_id,))
        db.execute("DELETE FROM warehouse_member WHERE user_id=%s", (member_id,))
        db.execute("UPDATE app_user SET is_active=false WHERE id=%s", (member_id,))
    return Response(status_code=204)
