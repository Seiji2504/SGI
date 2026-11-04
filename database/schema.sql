-- Menoss AWS schema: PostgreSQL 15+ (Amazon RDS for PostgreSQL).
-- Run once as the database migration owner. Application uses a restricted DB role.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS app_user (
    id uuid PRIMARY KEY,
    email text NOT NULL DEFAULT '',
    full_name text NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS warehouse (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    created_by uuid NOT NULL REFERENCES app_user(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS warehouse_member (
    warehouse_id uuid NOT NULL REFERENCES warehouse(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, user_id)
);

CREATE TABLE IF NOT EXISTS product (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id uuid NOT NULL REFERENCES warehouse(id),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    category text NOT NULL DEFAULT 'General' CHECK (length(trim(category)) BETWEEN 1 AND 80),
    sku text CHECK (sku IS NULL OR length(sku) <= 50),
    unit text NOT NULL DEFAULT 'unidad' CHECK (length(trim(unit)) BETWEEN 1 AND 24),
    price numeric(10,2) NOT NULL DEFAULT 0 CHECK (price >= 0),
    created_by uuid NOT NULL REFERENCES app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    CONSTRAINT product_warehouse_sku_unique UNIQUE (warehouse_id, sku)
);

CREATE TABLE IF NOT EXISTS product_detail (
    product_id uuid PRIMARY KEY REFERENCES product(id),
    quantity integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    min_stock integer NOT NULL DEFAULT 5 CHECK (min_stock >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory_movement (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id uuid NOT NULL REFERENCES warehouse(id),
    product_id uuid NOT NULL REFERENCES product(id),
    user_id uuid NOT NULL REFERENCES app_user(id),
    type text NOT NULL CHECK (type IN ('entrada','salida','ajuste')),
    quantity integer NOT NULL CHECK (quantity >= 0),
    previous_stock integer NOT NULL CHECK (previous_stock >= 0),
    new_stock integer NOT NULL CHECK (new_stock >= 0),
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (type = 'ajuste' OR quantity > 0)
);

CREATE INDEX IF NOT EXISTS product_warehouse_name_idx
    ON product (warehouse_id, name, id) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS product_warehouse_category_idx
    ON product (warehouse_id, category) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS product_search_name_idx
    ON product USING gin (name gin_trgm_ops)
    WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS product_search_sku_idx
    ON product USING gin (coalesce(sku,'') gin_trgm_ops)
    WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS product_detail_low_stock_idx
    ON product_detail (product_id) WHERE quantity <= min_stock;
CREATE INDEX IF NOT EXISTS inventory_movement_product_date_idx
    ON inventory_movement (warehouse_id, product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS warehouse_member_user_idx
    ON warehouse_member (user_id, created_at);

-- Public signup can see warehouse names. Enrollment itself is serialized and
-- assigns the first member as owner; all later members join as regular members.
CREATE OR REPLACE FUNCTION list_registration_warehouses()
RETURNS TABLE(id uuid, name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT w.id, w.name FROM public.warehouse w ORDER BY lower(w.name), w.id;
$$;
REVOKE ALL ON FUNCTION list_registration_warehouses() FROM PUBLIC;

CREATE OR REPLACE FUNCTION register_current_user(target_warehouse uuid, target_name text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    current_user_id uuid := nullif(current_setting('app.user_id', true), '')::uuid;
    member_count integer;
    assigned_role text;
BEGIN
    IF current_user_id IS NULL THEN
        RAISE EXCEPTION 'Autenticación requerida' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.app_user WHERE id = current_user_id AND is_active = true;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Cuenta desactivada' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM public.warehouse WHERE id = target_warehouse FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Bodega no encontrada' USING ERRCODE = 'P0002';
    END IF;
    IF EXISTS (SELECT 1 FROM public.warehouse_member WHERE user_id = current_user_id) THEN
        RAISE EXCEPTION 'La cuenta ya pertenece a una bodega' USING ERRCODE = '23505';
    END IF;
    UPDATE public.app_user SET full_name = trim(target_name) WHERE id = current_user_id;
    SELECT count(*) INTO member_count FROM public.warehouse_member WHERE warehouse_id = target_warehouse;
    assigned_role := CASE WHEN member_count = 0 THEN 'owner' ELSE 'member' END;
    INSERT INTO public.warehouse_member(warehouse_id, user_id, role)
    VALUES (target_warehouse, current_user_id, assigned_role);
    RETURN assigned_role;
END;
$$;
REVOKE ALL ON FUNCTION register_current_user(uuid, text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION is_platform_admin() RETURNS boolean
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT coalesce(nullif(current_setting('app.platform_admin', true), '')::boolean, false);
$$;

-- SECURITY DEFINER helpers let the restricted runtime role enforce membership
-- checks without granting it cross-tenant access to warehouse_member rows.
CREATE OR REPLACE FUNCTION can_access_warehouse(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT public.is_platform_admin() OR EXISTS (
        SELECT 1 FROM public.warehouse_member wm
        WHERE wm.warehouse_id = target
          AND wm.user_id = nullif(current_setting('app.user_id', true), '')::uuid
    );
$$;
CREATE OR REPLACE FUNCTION can_write_warehouse(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT public.is_platform_admin() OR EXISTS (
        SELECT 1 FROM public.warehouse_member wm
        WHERE wm.warehouse_id = target
          AND wm.warehouse_id = nullif(current_setting('app.warehouse_id', true), '')::uuid
          AND wm.user_id = nullif(current_setting('app.user_id', true), '')::uuid
          AND wm.role <> 'viewer'
    );
$$;
CREATE OR REPLACE FUNCTION can_access_current_warehouse(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT target = nullif(current_setting('app.warehouse_id', true), '')::uuid
       AND public.can_access_warehouse(target);
$$;
CREATE OR REPLACE FUNCTION can_manage_warehouse(target uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT public.is_platform_admin() OR EXISTS (
        SELECT 1 FROM public.warehouse_member wm
        WHERE wm.warehouse_id = target
          AND wm.user_id = nullif(current_setting('app.user_id', true), '')::uuid
          AND wm.role IN ('owner','admin')
    );
$$;

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE warehouse ENABLE ROW LEVEL SECURITY;
ALTER TABLE warehouse_member ENABLE ROW LEVEL SECURITY;
ALTER TABLE product ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_detail ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_movement ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS app_user_self ON app_user;
CREATE POLICY app_user_self ON app_user USING (id = nullif(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (id = nullif(current_setting('app.user_id', true), '')::uuid);
DROP POLICY IF EXISTS app_user_platform_admin ON app_user;
CREATE POLICY app_user_platform_admin ON app_user FOR ALL
    USING (public.is_platform_admin()) WITH CHECK (public.is_platform_admin());
DROP POLICY IF EXISTS app_user_warehouse_manager_read ON app_user;
CREATE POLICY app_user_warehouse_manager_read ON app_user FOR SELECT USING (
    EXISTS (
      SELECT 1 FROM public.warehouse_member wm
      WHERE wm.user_id = app_user.id
        AND wm.warehouse_id = nullif(current_setting('app.warehouse_id', true), '')::uuid
        AND public.can_manage_warehouse(wm.warehouse_id)
    )
);
DROP POLICY IF EXISTS warehouse_member_read ON warehouse_member;
CREATE POLICY warehouse_member_read ON warehouse_member FOR SELECT
    USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid OR can_manage_warehouse(warehouse_id));
DROP POLICY IF EXISTS warehouse_member_write ON warehouse_member;
CREATE POLICY warehouse_member_write ON warehouse_member FOR INSERT
    WITH CHECK (
      can_manage_warehouse(warehouse_id)
    );
DROP POLICY IF EXISTS warehouse_member_update ON warehouse_member;
CREATE POLICY warehouse_member_update ON warehouse_member FOR UPDATE
    USING (can_manage_warehouse(warehouse_id)) WITH CHECK (can_manage_warehouse(warehouse_id));
DROP POLICY IF EXISTS warehouse_member_delete ON warehouse_member;
CREATE POLICY warehouse_member_delete ON warehouse_member FOR DELETE USING (can_manage_warehouse(warehouse_id));
DROP POLICY IF EXISTS warehouse_read ON warehouse;
CREATE POLICY warehouse_read ON warehouse FOR SELECT USING (
    can_access_warehouse(id)
    OR created_by = nullif(current_setting('app.user_id', true), '')::uuid
);
DROP POLICY IF EXISTS warehouse_create ON warehouse;
CREATE POLICY warehouse_create ON warehouse FOR INSERT
    WITH CHECK (public.is_platform_admin());
DROP POLICY IF EXISTS warehouse_update ON warehouse;
CREATE POLICY warehouse_update ON warehouse FOR UPDATE USING (can_manage_warehouse(id))
    WITH CHECK (can_manage_warehouse(id));
DROP POLICY IF EXISTS warehouse_platform_admin_delete ON warehouse;
CREATE POLICY warehouse_platform_admin_delete ON warehouse FOR DELETE USING (public.is_platform_admin());
DROP POLICY IF EXISTS product_tenant_access ON product;
CREATE POLICY product_tenant_access ON product USING (can_access_current_warehouse(warehouse_id))
    WITH CHECK (can_write_warehouse(warehouse_id));
DROP POLICY IF EXISTS product_detail_tenant_access ON product_detail;
CREATE POLICY product_detail_tenant_access ON product_detail USING (
    EXISTS (SELECT 1 FROM product p WHERE p.id = product_id AND can_access_current_warehouse(p.warehouse_id))
) WITH CHECK (
    EXISTS (SELECT 1 FROM product p WHERE p.id = product_id AND can_write_warehouse(p.warehouse_id))
);
DROP POLICY IF EXISTS movement_tenant_access ON inventory_movement;
CREATE POLICY movement_tenant_access ON inventory_movement USING (can_access_current_warehouse(warehouse_id))
    WITH CHECK (can_write_warehouse(warehouse_id) AND user_id = nullif(current_setting('app.user_id', true), '')::uuid);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS app_user_set_updated_at ON app_user;
CREATE TRIGGER app_user_set_updated_at BEFORE UPDATE ON app_user
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS product_set_updated_at ON product;
CREATE TRIGGER product_set_updated_at BEFORE UPDATE ON product
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS product_detail_set_updated_at ON product_detail;
CREATE TRIGGER product_detail_set_updated_at BEFORE UPDATE ON product_detail
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Create an application runtime role separately and grant only required DML.
-- Example (run with a DBA-managed password, never commit it):
-- CREATE ROLE menoss_app LOGIN PASSWORD '...';
-- GRANT CONNECT ON DATABASE menoss TO menoss_app;
-- GRANT USAGE ON SCHEMA public TO menoss_app;
-- GRANT SELECT, INSERT, UPDATE ON app_user, warehouse, product,
--   product_detail, inventory_movement TO menoss_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON warehouse_member TO menoss_app;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO menoss_app;
