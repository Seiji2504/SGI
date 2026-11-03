\set ON_ERROR_STOP on
SELECT format('CREATE ROLE menoss_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'menoss_app')
\gexec
ALTER ROLE menoss_app LOGIN PASSWORD :'app_password';
\ir schema.sql
GRANT CONNECT ON DATABASE :"DBNAME" TO menoss_app;
GRANT USAGE ON SCHEMA public TO menoss_app;
GRANT SELECT, INSERT, UPDATE ON app_user, warehouse, product,
  product_detail, inventory_movement TO menoss_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON warehouse_member TO menoss_app;
GRANT EXECUTE ON FUNCTION list_registration_warehouses() TO menoss_app;
GRANT EXECUTE ON FUNCTION register_current_user(uuid, text) TO menoss_app;
