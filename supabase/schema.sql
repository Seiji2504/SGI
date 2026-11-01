-- LEGACY: esquema de la versión anterior que usaba Supabase.
-- La aplicación AWS utiliza database/schema.sql y Cognito; no apliques este archivo a RDS.
-- Menoss · Esquema de base de datos
-- Ejecutar completo en Supabase > SQL Editor > New query.
-- Las credenciales y contraseñas se gestionan en Supabase Auth; public.usuario
-- guarda el perfil de la cuenta y referencia auth.users.

create extension if not exists pgcrypto;

-- Perfil de cada cuenta registrada en Supabase Auth.
create table if not exists public.usuario (
  id uuid primary key references auth.users(id) on delete cascade,
  nombre_completo text not null default '',
  correo text unique,
  creado_en timestamptz not null default now(),
  actualizado_en timestamptz not null default now()
);

-- Catálogo de productos por bodega/usuario.
create table if not exists public.producto (
  id uuid primary key default gen_random_uuid(),
  usuario_id uuid not null references public.usuario(id) on delete cascade,
  nombre text not null check (length(trim(nombre)) between 1 and 120),
  categoria text not null default 'General',
  codigo text,
  unidad text not null default 'unidad',
  precio numeric(10,2) not null default 0 check (precio >= 0),
  creado_en timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  constraint producto_usuario_codigo_unique unique (usuario_id, codigo)
);

-- Existencias y umbral de reposición del producto en una bodega.
create table if not exists public.detalle_producto (
  id uuid primary key default gen_random_uuid(),
  usuario_id uuid not null references public.usuario(id) on delete cascade,
  producto_id uuid not null references public.producto(id) on delete cascade,
  cantidad integer not null default 0 check (cantidad >= 0),
  stock_minimo integer not null default 5 check (stock_minimo >= 0),
  creado_en timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  constraint detalle_producto_usuario_producto_unique unique (usuario_id, producto_id)
);

-- Historial auditable de entradas, salidas y ajustes de stock.
create table if not exists public.movimiento_inventario (
  id uuid primary key default gen_random_uuid(),
  usuario_id uuid not null references public.usuario(id) on delete cascade,
  producto_id uuid not null references public.producto(id) on delete cascade,
  tipo text not null check (tipo in ('entrada', 'salida', 'ajuste')),
  cantidad integer not null check (cantidad >= 0),
  stock_anterior integer not null check (stock_anterior >= 0),
  stock_nuevo integer not null check (stock_nuevo >= 0),
  nota text,
  creado_en timestamptz not null default now()
);

create index if not exists producto_usuario_nombre_idx on public.producto(usuario_id, nombre);
create index if not exists producto_usuario_categoria_idx on public.producto(usuario_id, categoria);
create index if not exists detalle_producto_usuario_idx on public.detalle_producto(usuario_id);
create index if not exists movimiento_producto_fecha_idx on public.movimiento_inventario(usuario_id, producto_id, creado_en desc);

-- Mantiene la marca de tiempo al modificar filas.
create or replace function public.actualizar_fecha_modificacion()
returns trigger language plpgsql as $$
begin
  new.actualizado_en = now();
  return new;
end;
$$;

drop trigger if exists usuario_actualizado_en on public.usuario;
create trigger usuario_actualizado_en before update on public.usuario
for each row execute function public.actualizar_fecha_modificacion();
drop trigger if exists producto_actualizado_en on public.producto;
create trigger producto_actualizado_en before update on public.producto
for each row execute function public.actualizar_fecha_modificacion();
drop trigger if exists detalle_producto_actualizado_en on public.detalle_producto;
create trigger detalle_producto_actualizado_en before update on public.detalle_producto
for each row execute function public.actualizar_fecha_modificacion();

-- Crea/sincroniza el perfil cuando se registra o actualiza una cuenta Auth.
create or replace function public.sincronizar_usuario_desde_auth()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.usuario (id, nombre_completo, correo)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'full_name', ''),
    coalesce(new.email, '')
  )
  on conflict (id) do update
    set nombre_completo = coalesce(new.raw_user_meta_data ->> 'full_name', usuario.nombre_completo),
        correo = coalesce(new.email, usuario.correo);
  return new;
end;
$$;

drop trigger if exists al_crear_usuario_auth on auth.users;
create trigger al_crear_usuario_auth
after insert or update of email, raw_user_meta_data on auth.users
for each row execute function public.sincronizar_usuario_desde_auth();

-- Crea perfiles para cuentas que ya existían antes de aplicar este esquema.
insert into public.usuario (id, nombre_completo, correo)
select id, coalesce(raw_user_meta_data ->> 'full_name', ''), coalesce(email, '')
from auth.users
on conflict (id) do update
  set nombre_completo = excluded.nombre_completo,
      correo = excluded.correo;

-- Seguridad: las cuentas solo pueden acceder a su perfil, catálogo y stock.
alter table public.usuario enable row level security;
alter table public.producto enable row level security;
alter table public.detalle_producto enable row level security;

drop policy if exists "usuario_accede_a_su_perfil" on public.usuario;
create policy "usuario_accede_a_su_perfil" on public.usuario
for select to authenticated
using ((select auth.uid()) = id);

drop policy if exists "producto_propietario" on public.producto;
create policy "producto_propietario" on public.producto
for select to authenticated
using ((select auth.uid()) = usuario_id);

drop policy if exists "detalle_producto_propietario" on public.detalle_producto;
create policy "detalle_producto_propietario" on public.detalle_producto
for select to authenticated
using ((select auth.uid()) = usuario_id);

alter table public.movimiento_inventario enable row level security;
drop policy if exists "movimiento_inventario_propietario" on public.movimiento_inventario;
create policy "movimiento_inventario_propietario" on public.movimiento_inventario
for select to authenticated
using ((select auth.uid()) = usuario_id);

-- Actualiza la existencia y registra el movimiento de forma atómica.
-- Para tipo 'ajuste', cantidad representa el nuevo stock objetivo.
create or replace function public.registrar_movimiento_inventario(
  p_usuario_id uuid,
  p_producto_id uuid,
  p_tipo text,
  p_cantidad integer,
  p_nota text default null
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_detalle public.detalle_producto%rowtype;
  v_stock_nuevo integer;
  v_movimiento public.movimiento_inventario%rowtype;
begin
  if p_tipo is null or p_tipo not in ('entrada', 'salida', 'ajuste') then
    raise exception 'Tipo de movimiento inválido';
  end if;
  if p_cantidad is null or p_cantidad < 0 or (p_tipo in ('entrada', 'salida') and p_cantidad = 0) then
    raise exception 'La cantidad del movimiento no es válida';
  end if;

  select * into v_detalle
  from public.detalle_producto
  where usuario_id = p_usuario_id and producto_id = p_producto_id
  for update;

  if not found then
    raise exception 'Producto o detalle de inventario no encontrado';
  end if;

  if p_tipo = 'entrada' then
    v_stock_nuevo := v_detalle.cantidad + p_cantidad;
  elsif p_tipo = 'salida' then
    v_stock_nuevo := v_detalle.cantidad - p_cantidad;
    if v_stock_nuevo < 0 then
      raise exception 'La salida supera el stock disponible';
    end if;
  else
    v_stock_nuevo := p_cantidad;
  end if;

  update public.detalle_producto
  set cantidad = v_stock_nuevo
  where id = v_detalle.id;

  insert into public.movimiento_inventario
    (usuario_id, producto_id, tipo, cantidad, stock_anterior, stock_nuevo, nota)
  values
    (p_usuario_id, p_producto_id, p_tipo, p_cantidad, v_detalle.cantidad, v_stock_nuevo, nullif(trim(p_nota), ''))
  returning * into v_movimiento;

  return v_movimiento.id;
end;
$$;

revoke all on function public.registrar_movimiento_inventario(uuid, uuid, text, integer, text) from public, anon, authenticated;
grant execute on function public.registrar_movimiento_inventario(uuid, uuid, text, integer, text) to service_role;

revoke all on public.usuario, public.producto, public.detalle_producto, public.movimiento_inventario from anon, authenticated;
grant select on public.usuario, public.producto, public.detalle_producto, public.movimiento_inventario to authenticated;
grant all on public.usuario, public.producto, public.detalle_producto, public.movimiento_inventario to service_role;

-- Migración opcional desde la primera versión del proyecto (tabla `products`).
-- Copia los registros existentes y conserva la tabla anterior como respaldo.
do $$
begin
  if to_regclass('public.products') is not null then
    execute $migration$
      insert into public.producto
        (id, usuario_id, nombre, categoria, codigo, unidad, precio, creado_en, actualizado_en)
      select p.id, p.user_id, p.name, p.category, p.sku, p.unit, p.price, p.created_at, p.updated_at
      from public.products p
      join public.usuario u on u.id = p.user_id
      on conflict (id) do nothing
    $migration$;

    execute $migration$
      insert into public.detalle_producto
        (usuario_id, producto_id, cantidad, stock_minimo, creado_en, actualizado_en)
      select p.user_id, p.id, p.quantity, p.min_stock, p.created_at, p.updated_at
      from public.products p
      join public.usuario u on u.id = p.user_id
      on conflict (usuario_id, producto_id) do nothing
    $migration$;
  end if;
end;
$$;

-- Registra como stock de apertura las existencias que ya estaban cargadas
-- antes de habilitar el historial. Es idempotente al volver a ejecutar el SQL.
insert into public.movimiento_inventario
  (usuario_id, producto_id, tipo, cantidad, stock_anterior, stock_nuevo, nota)
select d.usuario_id, d.producto_id, 'ajuste', d.cantidad, 0, d.cantidad,
       'Stock de apertura al habilitar historial'
from public.detalle_producto d
where not exists (
  select 1 from public.movimiento_inventario m
  where m.usuario_id = d.usuario_id and m.producto_id = d.producto_id
);
