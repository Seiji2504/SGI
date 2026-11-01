\set ON_ERROR_STOP on
\ir schema.sql

SELECT format('CREATE ROLE menoss_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'menoss_app')
\gexec
ALTER ROLE menoss_app LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"DBNAME" TO menoss_app;
GRANT USAGE ON SCHEMA public TO menoss_app;
GRANT SELECT, INSERT, UPDATE ON app_user, warehouse, warehouse_member,
  product, product_detail, inventory_movement TO menoss_app;
