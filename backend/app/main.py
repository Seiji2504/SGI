from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from psycopg import Connection, errors
from uuid import UUID

from .config import get_settings
from .dependencies import CurrentUser, Database
from .schemas import Movement, MovementInput, Product, ProductInput, ProductPage, WarehouseCreate, WarehouseMemberInput

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
    if not row:
        raise HTTPException(status_code=403, detail="No tienes acceso a esta bodega")
    if write and row["role"] == "viewer":
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
    return {"id": user["id"], "email": user["email"], "full_name": row["full_name"] if row else ""}


@app.patch("/api/account")
def update_account(payload: dict, user: CurrentUser, db: Database) -> dict:
    full_name = str(payload.get("full_name", "")).strip()
    if len(full_name) > 120:
        raise HTTPException(status_code=422, detail="El nombre no puede superar 120 caracteres")
    db.execute("UPDATE app_user SET full_name=%s WHERE id=%s", (full_name, user["id"]))
    return {"full_name": full_name}


@app.get("/api/warehouses")
def list_warehouses(user: CurrentUser, db: Database) -> list[dict]:
    return db.execute(
        "SELECT w.id,w.name,wm.role FROM warehouse w JOIN warehouse_member wm ON wm.warehouse_id=w.id "
        "WHERE wm.user_id=%s ORDER BY w.created_at", (user["id"],),
    ).fetchall()


@app.post("/api/warehouses", status_code=201)
def create_warehouse(payload: WarehouseCreate, user: CurrentUser, db: Database) -> dict:
    name = payload.name
    with db.transaction():
        warehouse = db.execute("INSERT INTO warehouse(name,created_by) VALUES (%s,%s) RETURNING id,name",
                               (name, user["id"])).fetchone()
        db.execute("INSERT INTO warehouse_member(warehouse_id,user_id,role) VALUES (%s,%s,'owner')",
                   (warehouse["id"], user["id"]))
    return {**warehouse, "role": "owner"}


@app.post("/api/warehouses/{warehouse_id}/members", status_code=201)
def add_warehouse_member(warehouse_id: UUID, payload: WarehouseMemberInput, user: CurrentUser, db: Database) -> dict:
    owner = db.execute("SELECT 1 FROM warehouse_member WHERE warehouse_id=%s AND user_id=%s AND role IN ('owner','admin')",
                       (warehouse_id, user["id"])).fetchone()
    if not owner:
        raise HTTPException(status_code=403, detail="Solo propietarios y administradores pueden agregar miembros")
    member_id = str(payload.user_id)
    role = payload.role
    try:
        db.execute("INSERT INTO warehouse_member(warehouse_id,user_id,role) VALUES (%s,%s,%s) "
                   "ON CONFLICT (warehouse_id,user_id) DO UPDATE SET role=EXCLUDED.role",
                   (warehouse_id, member_id, role))
    except errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="La persona debe iniciar sesión al menos una vez antes de agregarla") from exc
    return {"warehouse_id": warehouse_id, "user_id": member_id, "role": role}
