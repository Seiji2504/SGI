CREATE ROLE menoss_app LOGIN PASSWORD 'menoss-local-password';
GRANT CONNECT ON DATABASE menoss TO menoss_app;
GRANT USAGE ON SCHEMA public TO menoss_app;
GRANT SELECT, INSERT, UPDATE ON app_user, warehouse, warehouse_member,
  product, product_detail, inventory_movement TO menoss_app;
